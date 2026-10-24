use cheetah_fast::extractors::{self, generic_fetch};
use cheetah_fast::{FetchConfig, Secret};
use clap::Parser;
use redis::aio::{ConnectionManager, ConnectionManagerConfig};
use reqwest::Client;
use serde::Serialize;
use std::net::IpAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tokio::sync::Semaphore;
use tracing::{error, info, warn};

#[derive(Serialize)]
struct StatusPayload<'a> {
    job_id: &'a str,
    status: &'static str,
    pages_total: u32,
    pages_completed: u32,
    results: Vec<ResultEntry<'a>>,
}

#[derive(Serialize)]
struct ResultEntry<'a> {
    success: bool,
    data: ResultData<'a>,
}

#[derive(Serialize)]
struct ResultData<'a> {
    content: &'a str,
    markdown: &'a str,
    metadata: ResultMetadata<'a>,
}

#[derive(Serialize)]
struct ResultMetadata<'a> {
    title: &'a str,
    description: &'a str,
    language: &'a str,
    status_code: u16,
    strategy: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    fallback_from: Option<&'a str>,
}

#[derive(Parser, Debug)]
#[command(name = "cheetah-fast", about = "Fast static page scraper worker")]
struct Args {
    // Holds the password, so it is a `Secret`: trimmed on parse and never printed
    // by `{:?}`. A plain comment, not `///`, which clap would show in `--help`.
    #[arg(
        long,
        env = "REDIS_URL",
        default_value = "redis://127.0.0.1:6379",
        hide_env_values = true
    )]
    redis_url: Secret,

    #[arg(long, default_value_t = 50)]
    concurrency: usize,

    #[arg(long, default_value = "fast-workers")]
    group_name: String,

    #[arg(long, default_value = "scrape_jobs")]
    stream: String,

    #[arg(long, default_value = "job_results")]
    results_prefix: String,

    /// Optional GitHub API token that raises the GitHub extractor's rate limit.
    /// Prefer the GITHUB_TOKEN environment variable: a flag value is visible in
    /// `ps`. Use a fine-grained read-only token; the extractor refuses
    /// non-public repositories either way.
    #[arg(long, env = "GITHUB_TOKEN", hide_env_values = true)]
    github_token: Option<Secret>,
}

/// The `host:port` of a Redis URL, for logging. The user and password are
/// dropped by taking what follows the LAST `@` in the whole string, before any
/// path/query/fragment is cut: an unescaped `/`, `?` or `#` in the password must
/// not end the userinfo early and leave a password prefix in the output.
fn redis_host(url: &str) -> &str {
    let rest = url.split_once("://").map_or(url, |(_, r)| r);
    let hostpart = rest.rsplit_once('@').map_or(rest, |(_, h)| h);
    hostpart.split(['/', '?', '#']).next().unwrap_or(hostpart)
}

/// Opens the worker's Redis connection as a `ConnectionManager`, which replaces a
/// dropped connection by itself. A plain multiplexed connection does not: once
/// Redis restarts, fails over, or the socket is cut, every later command on it
/// fails with "broken pipe" until the process restarts, and nothing here would
/// notice. Clones share the one healing connection, so in-flight tasks recover too.
///
/// `XREADGROUP` blocks for up to 2 s, so the response timeout leaves ample
/// headroom; it only exists to bound a half-open socket that never answers.
async fn connect_redis(url: &str) -> redis::RedisResult<ConnectionManager> {
    let client = redis::Client::open(url)?;
    let config = ConnectionManagerConfig::new()
        .set_connection_timeout(Duration::from_secs(5))
        .set_response_timeout(Duration::from_secs(10));
    ConnectionManager::new_with_config(client, config).await
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "cheetah_fast=info".into()),
        )
        .init();

    let args = Args::parse();
    let consumer_name = format!("worker-{}", uuid::Uuid::new_v4());

    info!(
        redis_host = %redis_host(args.redis_url.expose()),
        concurrency = args.concurrency,
        group = %args.group_name,
        consumer = %consumer_name,
        "starting cheetah-fast worker"
    );

    let mut conn = connect_redis(args.redis_url.expose()).await?;

    let _: Result<(), _> = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&args.stream)
        .arg(&args.group_name)
        .arg("0")
        .arg("MKSTREAM")
        .query_async(&mut conn)
        .await;

    let http_client = Client::builder()
        .pool_max_idle_per_host(20)
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let config = Arc::new(FetchConfig {
        github_token: args.github_token.filter(|t| !t.expose().is_empty()),
        ..FetchConfig::default()
    });
    let semaphore = Arc::new(Semaphore::new(args.concurrency));

    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

    tokio::spawn(async move {
        let ctrl_c = signal::ctrl_c();
        let mut sigterm = signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to register SIGTERM handler");

        tokio::select! {
            _ = ctrl_c => info!("received SIGINT, shutting down"),
            _ = sigterm.recv() => info!("received SIGTERM, shutting down"),
        }
        let _ = shutdown_tx.send(true);
    });

    info!("worker ready, consuming from stream '{}'", args.stream);

    loop {
        if *shutdown_rx.borrow() {
            break;
        }

        let result: redis::RedisResult<redis::streams::StreamReadReply> = redis::cmd("XREADGROUP")
            .arg("GROUP")
            .arg(&args.group_name)
            .arg(&consumer_name)
            .arg("COUNT")
            .arg(10)
            .arg("BLOCK")
            .arg(2000_u64)
            .arg("STREAMS")
            .arg(&args.stream)
            .arg(">")
            .query_async(&mut conn)
            .await;

        let reply = match result {
            Ok(r) => r,
            Err(e) => {
                warn!(error = %e, "XREADGROUP failed, retrying");
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                continue;
            }
        };

        for stream_key in &reply.keys {
            for entry in &stream_key.ids {
                let entry_id = entry.id.clone();
                let url = match entry.map.get("url") {
                    Some(redis::Value::BulkString(bytes)) => {
                        String::from_utf8_lossy(bytes).to_string()
                    }
                    _ => {
                        warn!(id = %entry_id, "job missing 'url' field, skipping");
                        let _: Result<(), _> = redis::cmd("XACK")
                            .arg(&args.stream)
                            .arg(&args.group_name)
                            .arg(&entry_id)
                            .query_async(&mut conn)
                            .await;
                        continue;
                    }
                };

                let job_id = entry
                    .map
                    .get("job_id")
                    .and_then(|v| match v {
                        redis::Value::BulkString(bytes) => {
                            Some(String::from_utf8_lossy(bytes).to_string())
                        }
                        _ => None,
                    })
                    .unwrap_or_else(|| entry_id.clone());

                let pinned_ip: Option<IpAddr> = entry
                    .map
                    .get("resolved_ip")
                    .and_then(|v| match v {
                        redis::Value::BulkString(bytes) => {
                            let s = String::from_utf8_lossy(bytes);
                            s.parse().ok()
                        }
                        _ => None,
                    });

                // Strategies this worker has no extractor for are ignored: the
                // page is fetched generically and reported as such.
                let requested_strategy = entry.map.get("strategy").and_then(|v| match v {
                    redis::Value::BulkString(bytes) if !bytes.is_empty() => {
                        Some(String::from_utf8_lossy(bytes).to_string())
                    }
                    _ => None,
                });

                let format = entry.map.get("format").and_then(|v| match v {
                    redis::Value::BulkString(bytes) => Some(String::from_utf8_lossy(bytes).to_string()),
                    _ => None,
                });
                let requested_strategy = requested_strategy
                    .as_deref()
                    .and_then(|s| extractors::strategy_for_format(Some(s), format.as_deref()))
                    .map(str::to_string);

                let permit = semaphore.clone().acquire_owned().await.unwrap();
                let client = http_client.clone();
                let cfg = config.clone();
                let mut task_conn = conn.clone();
                let stream_name = args.stream.clone();
                let group = args.group_name.clone();
                let results_prefix = args.results_prefix.clone();

                tokio::spawn(async move {
                    let _permit = permit;
                    info!(url = %url, job_id = %job_id, pinned_ip = ?pinned_ip, requested_strategy = ?requested_strategy, "processing job");

                    let outcome = extractors::run_chain(
                        &client,
                        &url,
                        &cfg,
                        pinned_ip,
                        requested_strategy.as_deref(),
                        extractors::lookup,
                        || generic_fetch(&client, &url, &cfg, pinned_ip),
                    )
                    .await;
                    let result = outcome.result;
                    let result_key = format!("{}:{}", results_prefix, job_id);

                    let (payload, title_buf, desc_buf, lang_buf);
                    let serialized = match result {
                        Ok(page) => {
                            info!(url = %url, job_id = %job_id, status = page.status_code, "job complete");
                            title_buf = page.title.unwrap_or_default();
                            desc_buf = page.description.unwrap_or_default();
                            lang_buf = page.language.unwrap_or_default();
                            payload = StatusPayload {
                                job_id: &job_id,
                                status: "completed",
                                pages_total: 1,
                                pages_completed: 1,
                                results: vec![ResultEntry {
                                    success: true,
                                    data: ResultData {
                                        content: &page.raw_html,
                                        markdown: &page.markdown,
                                        metadata: ResultMetadata {
                                            title: &title_buf,
                                            description: &desc_buf,
                                            language: &lang_buf,
                                            status_code: page.status_code,
                                            strategy: &outcome.strategy,
                                            fallback_from: outcome.fallback_from.as_deref(),
                                        },
                                    },
                                }],
                            };
                            serde_json::to_string(&payload).unwrap_or_default()
                        }
                        Err(e) => {
                            error!(url = %url, job_id = %job_id, error = %e, "job failed");
                            payload = StatusPayload {
                                job_id: &job_id,
                                status: "failed",
                                pages_total: 1,
                                pages_completed: 0,
                                results: vec![],
                            };
                            serde_json::to_string(&payload).unwrap_or_default()
                        }
                    };

                    let _: Result<(), _> = redis::cmd("SET")
                        .arg(&result_key)
                        .arg(&serialized)
                        .arg("EX")
                        .arg(3600_u64)
                        .query_async(&mut task_conn)
                        .await;

                    let _: Result<(), _> = redis::cmd("XACK")
                        .arg(&stream_name)
                        .arg(&group)
                        .arg(&entry_id)
                        .query_async(&mut task_conn)
                        .await;
                });
            }
        }
    }

    info!("shutdown complete");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpSocket};
    use tokio::sync::watch;

    /// Binds with SO_REUSEADDR so the same port can be taken again right after the
    /// previous listener (and its TIME_WAIT sockets) closed.
    fn bind(addr: std::net::SocketAddr) -> TcpListener {
        let sock = TcpSocket::new_v4().expect("socket");
        sock.set_reuseaddr(true).expect("reuseaddr");
        sock.bind(addr).expect("bind");
        sock.listen(16).expect("listen")
    }

    /// Stands in for Redis: answers every command with +PONG. Sending on the
    /// returned channel closes the listener and every open connection, which is
    /// what a Redis restart or failover looks like to a client. Needs no server.
    fn fake_redis(listener: TcpListener) -> watch::Sender<()> {
        let (stop_tx, stop_rx) = watch::channel(());
        tokio::spawn(async move {
            let mut accept_stop = stop_rx.clone();
            loop {
                tokio::select! {
                    _ = accept_stop.changed() => break,
                    accepted = listener.accept() => {
                        let Ok((mut sock, _)) = accepted else { break };
                        let mut conn_stop = stop_rx.clone();
                        tokio::spawn(async move {
                            let mut buf = [0u8; 1024];
                            loop {
                                tokio::select! {
                                    _ = conn_stop.changed() => break,
                                    n = sock.read(&mut buf) => match n {
                                        Ok(0) | Err(_) => break,
                                        Ok(n) => {
                                            // One reply per command: a client may pipeline
                                            // several in one write (redis-rs sends setup
                                            // commands on connect). Commands are RESP arrays,
                                            // each starting with '*' at the start of a line.
                                            let commands = (0..n)
                                                .filter(|&i| buf[i] == b'*' && (i == 0 || buf[i - 1] == b'\n'))
                                                .count()
                                                .max(1);
                                            let reply = b"+PONG\r\n".as_slice().repeat(commands);
                                            if sock.write_all(&reply).await.is_err() {
                                                break;
                                            }
                                        }
                                    },
                                }
                            }
                        });
                    }
                }
            }
        });
        stop_tx
    }

    async fn ping(conn: &mut ConnectionManager) -> redis::RedisResult<String> {
        redis::cmd("PING").query_async(conn).await
    }

    #[tokio::test]
    async fn redis_connection_recovers_after_the_server_drops_it() {
        let first = bind("127.0.0.1:0".parse().unwrap());
        let addr = first.local_addr().unwrap();
        let stop = fake_redis(first);

        let mut conn = tokio::time::timeout(
            Duration::from_secs(15),
            connect_redis(&format!("redis://{addr}")),
        )
        .await
        .expect("connect timed out")
        .expect("connect");
        assert_eq!(ping(&mut conn).await.expect("first ping"), "PONG");

        // The server goes away and a new one starts on the same address.
        stop.send(()).unwrap();
        tokio::time::sleep(Duration::from_millis(200)).await;
        let _second = fake_redis(bind(addr));

        // Commands may fail briefly while the manager notices the drop and
        // reconnects. It must then recover without a restart of the process.
        let mut recovered = false;
        for _ in 0..30 {
            if ping(&mut conn).await.is_ok() {
                recovered = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        assert!(
            recovered,
            "the Redis connection did not recover after the server restarted"
        );
    }

    #[test]
    fn redis_host_drops_credentials_and_path() {
        assert_eq!(
            redis_host("redis://default:s3cret@h.example:6379"),
            "h.example:6379"
        );
        assert_eq!(
            redis_host("redis://default:s3cret@h.example:6379/0"),
            "h.example:6379"
        );
        assert_eq!(redis_host("redis://h.example:6379"), "h.example:6379");
        assert_eq!(redis_host("localhost:6379"), "localhost:6379");
        // Unescaped '@', '/', '?' or '#' in the password must not leak a fragment.
        assert_eq!(
            redis_host("redis://u:p@ss@h.example:6379"),
            "h.example:6379"
        );
        assert_eq!(
            redis_host("redis://u:pa/ss@h.example:6379"),
            "h.example:6379"
        );
        assert_eq!(
            redis_host("redis://default:abc/def@h.example:6379/0"),
            "h.example:6379"
        );
        assert_eq!(
            redis_host("redis://u:p?w#x@h.example:6379"),
            "h.example:6379"
        );
        assert_eq!(redis_host("redis://h.example:6379?x=1"), "h.example:6379");
        assert_eq!(redis_host("redis://u:p@[::1]:6379"), "[::1]:6379");
    }

    #[test]
    fn redis_url_is_trimmed_and_not_debug_printed() {
        let args = Args::try_parse_from([
            "cheetah-fast",
            "--redis-url",
            "redis://u:pw@h.example:6379 ",
        ])
        .expect("args parse");
        assert_eq!(args.redis_url.expose(), "redis://u:pw@h.example:6379");
        let dbg = format!("{:?}", args);
        assert!(
            !dbg.contains("pw@"),
            "Debug leaked the redis password: {dbg}"
        );
    }

    #[test]
    fn result_metadata_reports_strategy() {
        let m = ResultMetadata {
            title: "t",
            description: "",
            language: "",
            status_code: 200,
            strategy: "generic",
            fallback_from: None,
        };
        let v: serde_json::Value = serde_json::to_value(&m).unwrap();
        assert_eq!(v["strategy"], "generic");
        assert!(v.get("fallback_from").is_none());

        let m = ResultMetadata {
            fallback_from: Some("github_repo"),
            ..m
        };
        let v: serde_json::Value = serde_json::to_value(&m).unwrap();
        assert_eq!(v["fallback_from"], "github_repo");
    }
}
