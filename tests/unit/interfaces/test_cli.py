import os
import subprocess
import sys


def test_cli_traces_and_backfill_help_runs():
    # Just verify commands initialize without crashing; not full E2E
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    cwd = os.getcwd()
    env["PYTHONPATH"] = f"{cwd}{os.pathsep}{existing}" if existing else cwd
    cmds = [
        [sys.executable, "-m", "motet.cli", "--help"],
        [sys.executable, "-m", "motet.cli", "version", "--help"],
        [sys.executable, "-m", "motet.cli", "traces", "--help"],
        [sys.executable, "-m", "motet.cli", "database", "migrate-pgvector", "--help"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, check=True, env=env)


