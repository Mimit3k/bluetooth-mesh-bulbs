"""Run the offline regression suites.

The suites are standalone scripts: they create temporary mesh states, never
touch Bluetooth hardware, and assert at import time. Running each in its own
interpreter keeps the Qt applications and module-level state isolated.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = sorted((ROOT / "tests" / "suites").glob("check_*.py"))


@pytest.mark.parametrize("script", SUITES, ids=[path.stem for path in SUITES])
def test_offline_suite(script: Path, tmp_path: Path) -> None:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Keep every TemporaryDirectory the suite creates inside pytest's tmp_path,
    # so a run leaves no state files or keys behind.
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"{script.name} failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "passed" in result.stdout.lower(), result.stdout
