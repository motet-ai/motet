import subprocess
import sys


def test_reflect_cli_help_smoke():
    # Ensure CLI is importable and command exists; ask for help to avoid running commands
    result = subprocess.run([sys.executable, "-m", "motet.cli", "--help"], capture_output=True)
    assert result.returncode == 0


