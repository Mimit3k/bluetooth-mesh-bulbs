"""Offline checks for SEQ recovery after an unclean shutdown.

A crash can leave the saved cursor behind the highest SEQ the nodes accepted,
and every later message is then dropped as a replay. The run marker left next
to the state file makes the next start jump forward; this suite proves both the
jump and the clean-shutdown path.
"""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import meshcore


def marker_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".running")


with tempfile.TemporaryDirectory(prefix="bulbctl-seq-") as tmp:
    path = Path(tmp) / "state.json"

    state = meshcore.State(path)
    state.data["seq"] = 1000
    state.save()
    assert marker_for(path).exists()
    state.release()
    assert not marker_for(path).exists(), "a clean release must remove the marker"

    # A clean reopen keeps the cursor exactly where it was.
    clean = meshcore.State(path)
    assert clean.data["seq"] == 1000
    clean.release()
    assert not marker_for(path).exists()

    # A crash leaves the marker behind. The next open must jump past anything
    # the nodes may already have accepted from this controller.
    marker_for(path).write_text("99999\n", encoding="ascii")
    recovered = meshcore.State(path)
    expected = 1000 + meshcore.State.CRASH_SEQ_MARGIN
    assert recovered.data["seq"] == expected, (recovered.data["seq"], expected)
    assert recovered.data["seq"] == 11000
    recovered.release()
    assert not marker_for(path).exists()

    # The jump is capped at the 24-bit SEQ maximum.
    capped = meshcore.State(path)
    capped.data["seq"] = 0xFFFFF0
    capped.save()
    capped.release()
    marker_for(path).write_text("99999\n", encoding="ascii")
    near_max = meshcore.State(path)
    assert near_max.data["seq"] == 0xFFFFFF
    near_max.release()

print("SEQ recovery tests passed")
