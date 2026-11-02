"""Offline regression test for the GUI's single-proxy-session rule.

Some Bluetooth adapters service only one GATT connection at a time.  Opening
a second connection to another bulb's proxy can silence both sessions, which
showed up as "the slider and colour stopped working" after switching bulbs.
The worker must keep one ``Link`` and retarget it, not cache one per bulb.
"""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import bulbgui
import meshcore

FIRST = 0x0002
SECOND = 0x0022


def make_state(root: Path) -> meshcore.State:
    state = meshcore.State(root / "state.json")
    state.add_device(
        FIRST,
        name="Bulb 1",
        uuid="11" * 16,
        address="F0:D1:B8:1D:65:3B",
        device_key=b"\x11" * 16,
        bound_models=[0x1000, 0x1300, 0x1307],
    )
    state.add_device(
        SECOND,
        name="Bulb 2",
        uuid="22" * 16,
        address="F0:D1:B8:1E:31:C6",
        device_key=b"\x22" * 16,
        bound_models=[0x1000, 0x1300, 0x1307],
    )
    state.save()
    return state


root = Path(tempfile.mkdtemp(prefix="bulbctl-session-test-"))
state = make_state(root)

# --- 1. set_target moves one open session to another node ------------------
link = meshcore.Link(state, FIRST)
link._range = (1, 100)
link._temp_range = (2000, 6500)
link._level = 1234

added: list[tuple[int, bytes]] = []


class FakeNode:
    def add_device(self, unicast: int, device_key: bytes) -> None:
        added.append((unicast, device_key))


link.node = FakeNode()
link.set_target(SECOND)

assert link.unicast == SECOND
assert link._range is None and link._temp_range is None and link._level is None
assert added == [(SECOND, b"\x22" * 16)], added

# Setting the same target again must not touch the session or the node.
link.set_target(SECOND)
assert added == [(SECOND, b"\x22" * 16)], added

# --- 2. The GUI worker reuses one Link for every bulb ---------------------
instances: list[object] = []


class FakeLink:
    def __init__(self, state, unicast, log_fn=None) -> None:
        self.unicast = unicast
        self.connected = False
        self.targets = [unicast]
        instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    def set_target(self, unicast: int) -> None:
        self.unicast = unicast
        self.targets.append(unicast)

    async def close(self) -> None:
        self.connected = False


real_link = meshcore.Link
meshcore.Link = FakeLink  # type: ignore[assignment]
try:
    # A separate state file: this test's own State holds its lock open.
    worker_root = root / "worker"
    worker_root.mkdir()
    worker = bulbgui.MeshWorker(str(worker_root / "state.json"))
    first = asyncio.run(worker._link(FIRST))
    second = asyncio.run(worker._link(SECOND))
    back = asyncio.run(worker._link(FIRST))

    assert first is second is back, "worker must not open a link per bulb"
    assert len(instances) == 1, [id(item) for item in instances]
    assert instances[0].targets == [FIRST, SECOND, FIRST], instances[0].targets
    assert worker.link is instances[0]

    # A closed session is replaced, never duplicated.
    asyncio.run(worker._drop())
    assert worker.link is None
    replacement = asyncio.run(worker._link(SECOND))
    assert replacement is not instances[0]
    assert len(instances) == 2
finally:
    meshcore.Link = real_link  # type: ignore[assignment]

worker.state.release()
state.release()

print("single-session regression test passed")
