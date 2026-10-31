"""Offscreen GUI and worker checks for configuration import/export."""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

from PyQt6.QtWidgets import QApplication

import bulbgui
import meshcore

app = QApplication([])
app.setStyleSheet(bulbgui.STYLE)
root = Path(tempfile.mkdtemp(prefix="bulbgui-config-test-"))

# --- GUI controls exist and follow the selection ------------------------
worker = bulbgui.MeshWorker(str(root / "gui-state.json"))
worker.state.add_device(
    0x0002,
    name="Desk",
    uuid="11" * 16,
    address="AA:BB:CC:DD:EE:02",
    device_key=bytes.fromhex("11" * 16),
    bound_models=[0x1000, 0x1300, 0x1303, 0x1307],
)
window = bulbgui.MainWindow(worker)
assert window.export_button.menu() is not None
export_actions = [a.text() for a in window.export_button.menu().actions()]
assert export_actions == ["Whole mesh…", "Selected bulb…"], export_actions
assert window.import_button.isEnabled()

window.on_devices(worker.state.snapshot())
assert window.selected == 0x0002
assert window.export_bulb_action.isEnabled()

window.on_devices([])
assert window.selected is None
assert not window.export_bulb_action.isEnabled()

window.on_devices(worker.state.snapshot())
assert window.export_bulb_action.isEnabled()

# --- worker coroutines actually write and apply files -------------------
worker2 = bulbgui.MeshWorker(str(root / "worker-state.json"))
worker2.state.add_device(
    0x0002,
    name="Desk",
    uuid="22" * 16,
    address="AA:BB:CC:DD:EE:02",
    device_key=bytes.fromhex("22" * 16),
    bound_models=[0x1000, 0x1300],
)
worker2.state.data["seq"] = 777
worker2.state.save()
mesh_file = root / "worker-export.json"
bulb_file = root / "worker-bulb.json"


async def worker_round_trip():
    await worker2._export_config(str(mesh_file), "mesh", None)
    await worker2._export_config(str(bulb_file), "bulb", 0x0002)
    package = meshcore.read_config(mesh_file)
    assert package["mesh"]["seq"] == 777
    assert set(package["devices"]) == {"0x0002"}
    assert meshcore.read_config(bulb_file)["kind"] == "bulb"

    imported = bulbgui.MeshWorker(str(root / "worker-target.json"))
    await imported._import_config(package, False)
    assert imported.state.data["seq"] == 777
    assert set(imported.state.devices) == {"0x0002"}
    imported.shutdown()


asyncio.run(worker_round_trip())


# --- forgetting is immediate, even if closing a stale link is slow ------
async def removal_is_immediate():
    seen = []
    original_drop = worker2._drop

    async def slow_drop(unicast):
        await asyncio.sleep(30)

    worker2._drop = slow_drop
    worker2.devices_changed.connect(
        lambda devices: seen.append([d["unicast"] for d in devices])
    )
    task = asyncio.create_task(worker2._remove(0x0002))
    await asyncio.sleep(0.05)
    assert seen and seen[-1] == [], seen
    assert "0x0002" not in worker2.state.devices
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    worker2._drop = original_drop


asyncio.run(removal_is_immediate())

window.poll_timer.stop()
window.temp_timer.stop()
worker.shutdown()
window.close()

print("configuration GUI/worker tests passed")
