import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def run_module(**env):
    return subprocess.run(
        [sys.executable, "-m", "transcript_worker"],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_python_dash_m_runs_main_and_a_bad_setting_exits_2_without_connecting():
    done = run_module(TRANSCRIPT_CONCURRENCY="0", REDIS_URL="redis://:s3cret@127.0.0.1:1")
    assert done.returncode == 2
    assert "TRANSCRIPT_CONCURRENCY" in done.stderr and "s3cret" not in done.stderr + done.stdout
