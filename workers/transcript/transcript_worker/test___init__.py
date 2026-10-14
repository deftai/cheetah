import importlib
import pkgutil

import transcript_worker


def test_every_module_imports_cleanly_and_has_a_docstring():
    names = [m.name for m in pkgutil.iter_modules(transcript_worker.__path__) if not m.name.startswith("test_")]
    assert {"captions", "errors", "ratelimit", "render", "tracks", "validators"} <= set(names)
    for name in names:
        module = importlib.import_module(f"transcript_worker.{name}")
        assert module.__doc__, f"{name} has no module docstring"


def test_the_core_has_no_runtime_dependencies():
    # The core stays pure: the network, Redis and yt-dlp belong to the runtime slice.
    banned = {"redis", "yt_dlp", "requests", "httpx", "aiohttp", "socket"}
    for m in pkgutil.iter_modules(transcript_worker.__path__):
        if m.name.startswith("test_"):
            continue
        source = (transcript_worker.__path__[0] + f"/{m.name}.py")
        with open(source, encoding="utf-8") as fh:
            imported = {
                line.split()[1].split(".")[0]
                for line in fh
                if line.startswith(("import ", "from ")) and not line.startswith("from .")
            }
        assert not imported & banned, f"{m.name} imports {imported & banned}"
