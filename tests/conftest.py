"""Make the source and vendored libraries importable in ad-hoc pytest runs."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
