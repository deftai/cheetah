import pytest

from transcript_worker.config import Config, ConfigError


def load(env=None, hostname="host-1"):
    return Config.from_env(env or {}, hostname=hostname)


def test_defaults():
    c = load()
    assert c.redis_url == "redis://127.0.0.1:6379"
    assert c.consumer_name == "transcript-host-1"
    assert (c.concurrency, c.job_timeout, c.rate_interval, c.cache_ttl, c.max_attempts) == (2, 90.0, 3.0, 86400, 3)


def test_values_come_from_the_environment():
    c = load(
        {
            "REDIS_URL": "redis://redis:6379",
            "TRANSCRIPT_CONSUMER_NAME": "tw-0",
            "TRANSCRIPT_CONCURRENCY": "4",
            "TRANSCRIPT_JOB_TIMEOUT_SECONDS": "60",
            "TRANSCRIPT_RATE_INTERVAL_SECONDS": "0.5",
            "TRANSCRIPT_CACHE_TTL_SECONDS": "0",
            "TRANSCRIPT_MAX_ATTEMPTS": "1",
        }
    )
    assert (c.redis_url, c.consumer_name, c.concurrency, c.job_timeout) == ("redis://redis:6379", "tw-0", 4, 60.0)
    assert (c.rate_interval, c.cache_ttl, c.max_attempts) == (0.5, 0, 1)


def test_blank_values_mean_the_default():
    assert load({"TRANSCRIPT_CACHE_TTL_SECONDS": "  ", "REDIS_URL": ""}).cache_ttl == 86400


def test_zero_ttl_disables_the_cache():
    assert load({"TRANSCRIPT_CACHE_TTL_SECONDS": "0"}).cache_ttl == 0


@pytest.mark.parametrize(
    "var,bad",
    [
        ("TRANSCRIPT_CONCURRENCY", "0"),
        ("TRANSCRIPT_CONCURRENCY", "9"),
        ("TRANSCRIPT_CONCURRENCY", "two"),
        ("TRANSCRIPT_JOB_TIMEOUT_SECONDS", "5"),
        ("TRANSCRIPT_JOB_TIMEOUT_SECONDS", "101"),
        ("TRANSCRIPT_JOB_TIMEOUT_SECONDS", "nan"),
        ("TRANSCRIPT_RATE_INTERVAL_SECONDS", "0"),
        ("TRANSCRIPT_RATE_INTERVAL_SECONDS", "-1"),
        ("TRANSCRIPT_RATE_INTERVAL_SECONDS", "inf"),
        ("TRANSCRIPT_CACHE_TTL_SECONDS", "-1"),
        ("TRANSCRIPT_CACHE_TTL_SECONDS", "1.5"),
        ("TRANSCRIPT_CACHE_TTL_SECONDS", str(31 * 86400)),
        ("TRANSCRIPT_MAX_ATTEMPTS", "0"),
        ("TRANSCRIPT_MAX_ATTEMPTS", "6"),
    ],
)
def test_bad_values_fail_fast_naming_the_variable(var, bad):
    with pytest.raises(ConfigError) as e:
        load({var: bad})
    assert var in str(e.value)


def test_the_redis_url_is_never_echoed():
    with pytest.raises(ConfigError) as e:
        load({"REDIS_URL": "redis://:s3cret@host:99999999/x y"})
    assert "s3cret" not in str(e.value) and "REDIS_URL" in str(e.value)


@pytest.mark.parametrize("url", ["host:6379", "redis://h", "rediss://h:6380/1", "unix:///var/run/redis.sock"])
def test_redis_urls(url):
    assert load({"REDIS_URL": url}).redis_url == url


def test_consumer_name_is_sanitised_and_bounded():
    assert load({}, hostname="Pod_1.example").consumer_name == "transcript-Pod_1.example"
    assert load({}, hostname="a b\n$(x)").consumer_name == "transcript-a-b---x"
    assert len(load({}, hostname="h" * 500).consumer_name) <= 64
    assert load({}, hostname="").consumer_name == "transcript-worker"


def test_an_explicit_consumer_name_must_be_safe():
    with pytest.raises(ConfigError):
        load({"TRANSCRIPT_CONSUMER_NAME": "a b"})
    with pytest.raises(ConfigError):
        load({"TRANSCRIPT_CONSUMER_NAME": "x" * 65})


def test_a_bare_host_and_port_gets_a_scheme_for_the_client():
    assert load({"REDIS_URL": "redis:6379"}).client_url == "redis://redis:6379"
    assert load({"REDIS_URL": "rediss://h:6380/1"}).client_url == "rediss://h:6380/1"


@pytest.mark.parametrize(
    "url,secret",
    [("us:p\u2105w@host:6379", "p\u2105w"), ("user:hunter2@host:99999999", "hunter2"), ("hunter2@host:6379/x y", "hunter2")],
)
def test_a_schemeless_url_is_checked_too_and_never_echoed(url, secret):
    with pytest.raises(ConfigError) as e:
        load({"REDIS_URL": url})
    assert secret not in str(e.value) and "REDIS_URL" in str(e.value)


def test_repr_does_not_show_the_redis_url():
    assert "s3cret" not in repr(load({"REDIS_URL": "redis://:s3cret@h:6379"}))
