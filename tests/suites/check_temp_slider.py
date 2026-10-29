"""Offscreen regression test for the mode-aware temperature slider."""

# ruff: noqa: E402  (the path bootstrap must run before imports)

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

app = QApplication([])
app.setStyleSheet(bulbgui.STYLE)

state_dir = tempfile.mkdtemp(prefix="bulbgui-test-")
worker = bulbgui.MeshWorker(os.path.join(state_dir, "state.json"))
window = bulbgui.MainWindow(worker)

sent = []
window._send = lambda action, value: sent.append((action, value))

unicast = 0x0002
device = {
    "unicast": unicast,
    "name": "Test bulb",
    "address": "AA:BB:CC:DD:EE:FF",
    "has_colour": True,
    "has_temperature": True,
}
window.on_devices([device])


def status(**overrides):
    st = {
        "on": True,
        "level": 25,
        "level_pct": 50,
        "range": (1, 50),
        "mode": "temperature",
        "temperature": 2248,
        "temperature_range": (1000, 12000),
        "hue": None,
        "saturation": None,
    }
    st.update(overrides)
    return st


# Temperature mode shows the real value.
window.on_status(unicast, status())
assert window.temp_slider.value() == 2248, window.temp_slider.value()
assert window.temp_value.text() == "2248 K", window.temp_value.text()
assert window._temp_estimate is False
assert window.temp_hint.text() == "Warm ← → cool"

# Warm white and White presets send real CTL temperatures.
window.swatch_buttons[0].click()
assert sent[-1] == ("temperature", 2700), sent
assert window.temp_slider.value() == 2700
assert window.temp_value.text() == "2700 K"
assert window._temp_estimate is False

window.swatch_buttons[1].click()
assert sent[-1] == ("temperature", 4000), sent
assert window.temp_slider.value() == 4000
assert window.temp_value.text() == "4000 K"

# A saturated colour moves the slider to the nearest white point immediately.
window.swatch_buttons[2].click()
assert sent[-1] == ("colour", "#ff0000"), sent
assert 1200 <= window.temp_slider.value() <= 1350, window.temp_slider.value()
assert window.temp_value.text().startswith("≈ ")
assert window._temp_estimate is True
assert "Estimated" in window.temp_hint.text()

# A status read in colour mode keeps the estimate, ignoring stale temperature.
window.on_status(unicast, status(mode="colour", hue=0, saturation=100))
assert window.temp_slider.value() != 2248
assert window.temp_value.text().startswith("≈ ")
assert window._temp_estimate is True
assert "Estimated" in window.temp_hint.text()

# Green gives a different estimate, so the slider visibly follows the colour.
window.on_status(unicast, status(mode="colour", hue=120, saturation=100))
green_value = window.temp_slider.value()
assert 3400 <= green_value <= 3600, green_value
assert green_value != 1260

# Estimates are clamped to the lamp's real temperature range.
window.temp_slider.setRange(2000, 6500)
window.set_colour("#ff0000")
assert window.temp_slider.value() == 2000
assert window.temp_value.text() == "≈ 2000 K"
window.temp_slider.setRange(1000, 12000)

# Dragging the slider returns to real white/temperature mode.
window.temp_slider.setValue(5000)
assert window._temp_estimate is False
assert window.temp_value.text() == "5000 K"
assert window.temp_hint.text() == "Warm ← → cool"
assert window.temp_timer.isActive()

# A status poll that lands mid-drag must not snap the slider back.
window.on_status(unicast, status(mode="colour", hue=0, saturation=100))
assert window.temp_slider.value() == 5000
assert window._temp_estimate is False
assert window.temp_value.text() == "5000 K"
window.temp_timer.stop()

# An explicit temperature command also restores exact mode.
window.set_temperature(4000)
assert sent[-1] == ("temperature", 4000), sent
assert window.temp_slider.value() == 4000
assert window.temp_value.text() == "4000 K"
assert window._temp_estimate is False

# A later colour-mode status cannot drag the slider back to the old CTL value.
window.on_status(unicast, status(mode="colour", hue=240, saturation=100))
assert window.temp_slider.value() != 4000
assert window._temp_estimate is True

window.poll_timer.stop()
window.temp_timer.stop()
worker.shutdown()
window.close()

print("temperature-slider regression test passed")
