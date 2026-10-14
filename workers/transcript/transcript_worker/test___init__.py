import ast
import importlib
import pkgutil
from pathlib import Path

import transcript_worker

CORE = {"captions", "errors", "ratelimit", "render", "tracks", "validators"}
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
