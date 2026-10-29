"""Offscreen GUI checks for the controller-address control."""

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

from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

import bulbgui
import meshcore

app = QApplication([])
app.setStyleSheet(bulbgui.STYLE)
root = Path(tempfile.mkdtemp(prefix="bulbgui-controller-test-"))

worker = bulbgui.MeshWorker(str(root / "gui-state.json"))
window = bulbgui.MainWindow(worker)
assert window.controller_button.text() == "Controller 0x0001…", (
    window.controller_button.text()
)
assert "unicast" in window.controller_button.toolTip()

meshcore.set_controller_address(worker.state, 0x7FFF)
window._update_controller_label()
assert window.controller_button.text() == "Controller 0x7fff…", (
    window.controller_button.text()
)

# The dialog handler parses, confirms and dispatches a change.
requested: list[int] = []
worker.set_controller_address = lambda address: requested.append(address)
original_get = QInputDialog.getText
original_question = QMessageBox.question
original_critical = QMessageBox.critical
errors: list[str] = []
try:
    QInputDialog.getText = staticmethod(lambda *a, **k: ("0x7ffe", True))
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    window.change_controller_address()
    assert requested == [0x7FFE], requested

    QInputDialog.getText = staticmethod(lambda *a, **k: ("0x7ffd", True))
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Cancel
    )
    window.change_controller_address()
    assert requested == [0x7FFE], requested

    QInputDialog.getText = staticmethod(lambda *a, **k: ("0x8000", True))
    QMessageBox.critical = staticmethod(
        lambda _parent, _title, text: errors.append(text)
    )
    window.change_controller_address()
    assert errors and "controller address" in errors[-1], errors
    assert requested == [0x7FFE], requested
finally:
    QInputDialog.getText = original_get
    QMessageBox.question = original_question
    QMessageBox.critical = original_critical

# The worker changes the address on its asyncio side and reports errors.
worker2 = bulbgui.MeshWorker(str(root / "worker-state.json"))
errors: list[str] = []
worker2.config_error.connect(errors.append)

asyncio.run(worker2._set_controller_address(0x7FFF))
assert worker2.state.data["provisioner_addr"] == 0x7FFF
assert not errors

asyncio.run(worker2._set_controller_address(0x8000))
assert errors and "controller address" in errors[-1], errors
assert worker2.state.data["provisioner_addr"] == 0x7FFF

# The success slot updates the button and resets stale connection state.
meshcore.set_controller_address(worker.state, 0x7FFE)
original_information = QMessageBox.information
QMessageBox.information = staticmethod(lambda *a, **k: None)
try:
    window.online = {0x0002: True}
    window.states = {0x0002: {"on": True}}
    window.selected = 0x0002
    window.on_controller_changed(0x7FFE, "this computer now uses 0x7ffe")
finally:
    QMessageBox.information = original_information
assert window.controller_button.text() == "Controller 0x7ffe…", (
    window.controller_button.text()
)
assert not window.online and not window.states

worker2.shutdown()
window.poll_timer.stop()
window.temp_timer.stop()
worker.shutdown()
window.close()

print("controller-address GUI/worker tests passed")
