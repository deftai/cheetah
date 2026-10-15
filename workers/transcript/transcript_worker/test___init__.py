import ast
import importlib
import pkgutil
from pathlib import Path

import transcript_worker

CORE = {"captions", "errors", "ratelimit", "render", "tracks", "validators"}
# Runtime modules that do no I/O themselves: Redis and yt-dlp are reached only through a seam or a duck-typed client.
PURE_RUNTIME = {"config", "cache", "pipeline", "source", "results", "handler"}
IO_EDGES = {"ytdlp_source", "consumer", "main", "__main__"}
# The core stays pure: the network, Redis and yt-dlp belong to the runtime slice.
FORBIDDEN = {"redis", "yt_dlp", "requests", "httpx", "aiohttp", "socket", "subprocess", "urllib"}


def test_every_core_module_imports_cleanly_and_has_a_docstring():
    names = {m.name for m in pkgutil.iter_modules(transcript_worker.__path__) if not m.name.startswith("test_")}
    assert CORE <= names
    for name in CORE:
        module = importlib.import_module(f"transcript_worker.{name}")
        assert module.__doc__, f"{name} has no module docstring"


def test_core_modules_import_no_io_libraries():
    for name in CORE:
        tree = ast.parse((Path(transcript_worker.__path__[0]) / f"{name}.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & FORBIDDEN, f"{name} imports {imported & FORBIDDEN}"


def _imports(name):
    tree = ast.parse((Path(transcript_worker.__path__[0]) / f"{name}.py").read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_every_module_is_classified_so_a_new_one_has_to_pick_a_layer():
    names = {m.name for m in pkgutil.iter_modules(transcript_worker.__path__) if not m.name.startswith("test_")}
    assert names == CORE | PURE_RUNTIME | IO_EDGES


def test_the_runtime_logic_reaches_redis_and_yt_dlp_only_through_the_edges():
    for name in PURE_RUNTIME:
        assert not _imports(name) & {"redis", "yt_dlp", "requests", "httpx", "aiohttp", "subprocess"}, name
