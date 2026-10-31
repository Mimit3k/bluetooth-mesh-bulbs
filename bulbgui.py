#!/usr/bin/env python3
"""bulbgui — a small desktop GUI for Bluetooth SIG-mesh lamps.

Shares all mesh plumbing with ``bulbctl`` through :mod:`meshcore`:

  * every bulb in the collection is listed on the left
  * on/off, brightness, colour temperature and RGB colour on the right
  * "Add bulb" walks through factory-reset → scan → provision → configure
  * Export/import the mesh keys and bulbs for another computer
  * A per-computer controller address so two PCs can share the same bulbs

Bluetooth work runs on a background asyncio loop, so the window never blocks
while the proxy connection is being found or a slider is being applied.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import meshcore

# The two white presets use real CTL temperatures; the rest use HSL colour.
# The third item is the Kelvin value for white presets, or None for colours.
PRESETS = [
    ("Warm white", "#ffd7a8", 2700),
    ("White", "#ffffff", 4000),
    ("Red", "#ff0000", None),
    ("Orange", "#ff7f00", None),
    ("Yellow", "#ffff00", None),
    ("Green", "#00ff00", None),
    ("Cyan", "#00ffff", None),
    ("Blue", "#0000ff", None),
    ("Purple", "#8000ff", None),
    ("Pink", "#ff00aa", None),
]

STYLE = """
QWidget { font-size: 14px; color: #20232a; }
QMainWindow, QDialog { background: #f4f5f7; }
QFrame#sidebar { background: #ffffff; border-right: 1px solid #e2e4e8; }
QFrame#card { background: #ffffff; border: 1px solid #e2e4e8; border-radius: 12px; }
QLabel#title { font-size: 22px; font-weight: 600; }
QLabel#subtitle { color: #6b7280; }
QLabel#section { font-weight: 600; margin-top: 4px; }
QPushButton {
    background: #ffffff; border: 1px solid #d4d7dd; border-radius: 8px;
    padding: 7px 14px;
}
QPushButton:hover { background: #f0f2f5; }
QPushButton:pressed { background: #e6e9ee; }
QPushButton:disabled { color: #9aa0a6; background: #f7f8fa; }
QPushButton#primary { background: #2f6fed; color: #ffffff; border: none; }
QPushButton#primary:hover { background: #2560d4; }
QPushButton#primary:disabled { background: #b9c7e8; }
QPushButton#power { font-size: 16px; font-weight: 600; padding: 12px; }
QPushButton#power:checked { background: #fff4d6; border-color: #e8b93b; color: #7a5200; }
QListWidget { border: none; outline: 0; background: transparent; }
QListWidget::item { padding: 8px 6px; border-radius: 8px; }
QListWidget::item:selected { background: #e7efff; color: #17325f; }
QSlider::groove:horizontal { height: 6px; border-radius: 3px; background: #e3e6eb; }
QSlider::sub-page:horizontal { background: #2f6fed; border-radius: 3px; }
QSlider::handle:horizontal {
    width: 18px; margin: -7px 0; border-radius: 9px;
    background: #ffffff; border: 2px solid #2f6fed;
}
QPlainTextEdit {
    background: #fbfbfc; border: 1px solid #e2e4e8; border-radius: 8px;
    font-family: monospace; font-size: 12px; color: #4b5563;
}
QProgressBar { border: 1px solid #e2e4e8; border-radius: 6px; height: 12px; text-align: center; }
QProgressBar::chunk { background: #2f6fed; border-radius: 5px; }
"""


def make_dot(colour: str) -> QIcon:
    pix = QPixmap(14, 14)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 10, 10)
    painter.end()
    return QIcon(pix)


def readable_text(colour: QColor) -> str:
    # Rough luminance test: enough to keep swatch labels legible.
    luma = 0.299 * colour.red() + 0.587 * colour.green() + 0.114 * colour.blue()
    return "#1d1d1d" if luma > 155 else "#ffffff"


def temperature_colour(kelvin: int) -> QColor:
    """A rough black-body swatch for 1000–12000 K (UI preview only)."""
    kelvin = max(1000, min(12000, int(kelvin)))
    if kelvin <= 4000:
        start, end = QColor("#ff9a4d"), QColor("#fff3df")
        fraction = (kelvin - 1000) / 3000
    else:
        start, end = QColor("#fff3df"), QColor("#cfe3ff")
        fraction = (kelvin - 4000) / 8000
    return QColor(
        round(start.red() + (end.red() - start.red()) * fraction),
        round(start.green() + (end.green() - start.green()) * fraction),
        round(start.blue() + (end.blue() - start.blue()) * fraction),
    )


def _planckian_xy(kelvin: int) -> tuple[float, float]:
    """Approximate CIE 1931 chromaticity of a black body (Kim et al.)."""
    t = max(1000, min(12000, int(kelvin)))
    if t <= 4000:
        x = (
            -0.2661239e9 / t**3
            - 0.2343589e6 / t**2
            + 0.8776956e3 / t
            + 0.179910
        )
    else:
        x = (
            -3.0258469e9 / t**3
            + 2.1070379e6 / t**2
            + 0.2226347e3 / t
            + 0.240390
        )
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def colour_temperature(colour: QColor) -> int:
    """Nearest white-light Kelvin value for a colour (UI estimate).

    The temperature slider is a white-point control. While the lamp is in HSL
    colour mode we point it at the closest spot on the black-body locus, so
    saturated colours move it towards the warm or cool end instead of leaving
    a stale temperature behind.
    """

    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in (colour.red(), colour.green(), colour.blue()))
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    total = x + y + z
    if total <= 0.0:
        return 6500
    px, py = x / total, y / total

    best_k, best_d = 6500, float("inf")
    for kelvin in range(1000, 12001, 20):
        bx, by = _planckian_xy(kelvin)
        distance = (px - bx) ** 2 + (py - by) ** 2
        if distance < best_d:
            best_d, best_k = distance, kelvin
    return best_k


class BulbRow(QWidget):
    """Two-line bulb row: name above address/state."""

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(10)

        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(1)
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: 600;")
        self.name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.detail = QLabel()
        self.detail.setStyleSheet("color:#6b7280; font-size:12px;")
        self.detail.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text.addWidget(self.name)
        text.addWidget(self.detail)
        layout.addLayout(text, 1)

    def update_row(self, name: str, detail: str, dot_colour: str) -> None:
        self.name.setText(name)
        self.detail.setText(detail)
        self.dot.setPixmap(make_dot(dot_colour).pixmap(12, 12))


# -------------------------------------------------------------------- worker


class MeshWorker(QObject):
    """Owns the asyncio loop, the state file and the persistent BLE links."""

    devices_changed = pyqtSignal(list)
    status_ready = pyqtSignal(int, dict)
    scan_ready = pyqtSignal(list)
    added = pyqtSignal(int, str)
    log_line = pyqtSignal(str)
    failed = pyqtSignal(str)
    config_exported = pyqtSignal(str, str)
    config_imported = pyqtSignal(str)
    config_error = pyqtSignal(str)
    controller_changed = pyqtSignal(int, str)

    def __init__(self, state_path: str) -> None:
        super().__init__()
        self.state = meshcore.State(state_path)
        self.links: dict[int, meshcore.Link] = {}
        self._op_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    # -- loop plumbing ---------------------------------------------------
    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="mesh-asyncio", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro) -> None:
        if self._loop is None or self._stopped:
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        fut.add_done_callback(self._finished)

    def _finished(self, fut) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            pass
        except (meshcore.MeshCoreError, TimeoutError) as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            logging.getLogger("bulbgui").exception("worker task failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def shutdown(self) -> None:
        if self._loop is None or self._stopped:
            return
        self._stopped = True
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)
        self.state.release()

    # -- links -----------------------------------------------------------
    async def _link(self, unicast: int) -> meshcore.Link:
        link = self.links.get(unicast)
        if link is None or not link.connected:
            link = meshcore.Link(self.state, unicast, log_fn=self.log_line.emit)
            await link.connect()
            self.links[unicast] = link
        return link

    async def _close_all(self) -> None:
        for link in list(self.links.values()):
            try:
                await link.close()
            except Exception:
                pass
        self.links.clear()

    async def _drop(self, unicast: int) -> None:
        link = self.links.pop(unicast, None)
        if link:
            try:
                await link.close()
            except Exception:
                pass

    # -- public operations ----------------------------------------------
    def refresh_devices(self) -> None:
        self.submit(self._refresh_devices())

    async def _refresh_devices(self) -> None:
        self.devices_changed.emit(self.state.snapshot())

    def refresh_status(self, unicast: int) -> None:
        self.submit(self._status(unicast))

    async def _status(self, unicast: int) -> None:
        async with self._op_lock:
            link = await self._link(unicast)
            try:
                st = await link.status()
            except (TimeoutError, meshcore.MeshCoreError):
                # The proxy session can go stale while the bulb is idle.
                # Discard it so the next poll/action reconnects from scratch.
                await self._drop(unicast)
                raise
        st["name"] = self.state.device(unicast).get("name")
        self.status_ready.emit(unicast, st)

    def control(self, unicast: int, action: str, value=None) -> None:
        self.submit(self._control(unicast, action, value))

    async def _control(self, unicast: int, action: str, value) -> None:
        async with self._op_lock:
            link = await self._link(unicast)
            try:
                await self._apply(link, action, value)
            except (TimeoutError, meshcore.MeshCoreError):
                # The proxy can drop between commands; reconnect once.
                self.log_line.emit("connection lost, reconnecting…")
                await self._drop(unicast)
                link = await self._link(unicast)
                try:
                    await self._apply(link, action, value)
                except (TimeoutError, meshcore.MeshCoreError):
                    await self._drop(unicast)
                    raise
            try:
                st = await link.status()
            except (TimeoutError, meshcore.MeshCoreError):
                # The command itself succeeded; only the read-back failed.
                await self._drop(unicast)
                raise
        st["name"] = self.state.device(unicast).get("name")
        self.status_ready.emit(unicast, st)
        self.devices_changed.emit(self.state.snapshot())

    async def _apply(self, link: meshcore.Link, action: str, value) -> None:
        if action == "power":
            await link.set_power(bool(value))
        elif action == "level":
            await link.set_level(int(value))
        elif action == "temperature":
            await link.set_temperature(int(value))
        elif action == "colour":
            await link.set_colour(str(value))
        elif action == "hue_sat":
            hue, sat = value
            await link.set_colour_hs(hue, sat)
        else:
            raise meshcore.MeshCoreError(f"unknown action {action!r}")

    def scan_new(self) -> None:
        self.submit(self._scan_new())

    async def _scan_new(self) -> None:
        async with self._op_lock:
            await self._close_all()
            self.log_line.emit("scanning for unprovisioned bulbs (12 s)…")
            result = await meshcore.scan(self.state, 12.0)
        found = result["unprovisioned"]
        self.log_line.emit(f"scan finished: {len(found)} unprovisioned bulb(s)")
        self.scan_ready.emit(found)

    def add_bulb(self, uuid: str, name: str) -> None:
        self.submit(self._add_bulb(uuid, name))

    async def _add_bulb(self, uuid: str, name: str) -> None:
        async with self._op_lock:
            await self._close_all()
            self.log_line.emit(f"provisioning {name!r} — this can take a minute…")
            unicast = await meshcore.provision(
                self.state, uuid=uuid, name=name, log_fn=self.log_line.emit
            )
        self.log_line.emit(f"added {name!r} at 0x{unicast:04x}")
        self.added.emit(unicast, name)
        self.devices_changed.emit(self.state.snapshot())

    def rename(self, unicast: int, name: str) -> None:
        self.submit(self._rename(unicast, name))

    async def _rename(self, unicast: int, name: str) -> None:
        self.state.rename(unicast, name)
        self.devices_changed.emit(self.state.snapshot())

    def remove(self, unicast: int) -> None:
        self.submit(self._remove(unicast))

    async def _remove(self, unicast: int) -> None:
        async with self._op_lock:
            await self._drop(unicast)
            self.state.remove(unicast)
        self.devices_changed.emit(self.state.snapshot())

    # -- configuration files --------------------------------------------
    def export_config(
        self, path: str, *, kind: str, unicast: int | None = None
    ) -> None:
        self.submit(self._export_config(path, kind, unicast))

    async def _export_config(
        self, path: str, kind: str, unicast: int | None
    ) -> None:
        try:
            async with self._op_lock:
                summary = meshcore.export_config(
                    self.state, path, kind=kind, unicast=unicast
                )
        except Exception as exc:  # noqa: BLE001 - report to the GUI
            self.config_error.emit(str(exc) or type(exc).__name__)
            return
        self.config_exported.emit(path, summary)

    def import_config(self, package: dict, *, replace: bool = False) -> None:
        self.submit(self._import_config(package, replace))

    async def _import_config(self, package: dict, replace: bool) -> None:
        try:
            async with self._op_lock:
                await self._close_all()
                summary = meshcore.apply_config(
                    self.state, package, replace=replace
                )
        except Exception as exc:  # noqa: BLE001 - report to the GUI
            self.config_error.emit(str(exc) or type(exc).__name__)
            return
        self.config_imported.emit(summary)

    # -- controller identity --------------------------------------------
    def set_controller_address(self, address: int) -> None:
        self.submit(self._set_controller_address(address))

    async def _set_controller_address(self, address: int) -> None:
        try:
            async with self._op_lock:
                summary = meshcore.set_controller_address(self.state, address)
                await self._close_all()
        except Exception as exc:  # noqa: BLE001 - report to the GUI
            self.config_error.emit(str(exc) or type(exc).__name__)
            return
        self.controller_changed.emit(
            int(self.state.data["provisioner_addr"]), summary
        )


# ------------------------------------------------------------- add-bulb dialog


class AddBulbDialog(QDialog):
    def __init__(self, worker: MeshWorker, next_number: int, parent=None) -> None:
        super().__init__(parent)
        self.worker = worker
        self.unicast = None
        self._busy = False
        self.setWindowTitle("Add a bulb")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(12)

        title = QLabel("Add a bulb")
        title.setObjectName("title")
        root.addWidget(title)

        help_text = QLabel(
            "<b>1.</b> Factory-reset the bulb: switch it off and on "
            "<b>5 times in a row</b> (about 1 s on, 1 s off).<br>"
            "<b>2.</b> It should flash or pulse to show it is ready.<br>"
            "<b>3.</b> Keep it within a metre of this computer, then press Scan."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("subtitle")
        root.addWidget(help_text)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit(f"Bulb {next_number}")
        name_row.addWidget(self.name_edit, 1)
        root.addLayout(name_row)

        self.scan_button = QPushButton("Scan for new bulbs")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.start_scan)
        root.addWidget(self.scan_button)

        self.results = QListWidget()
        self.results.setMinimumHeight(110)
        self.results.setStyleSheet(
            "QListWidget { background:#ffffff; border:1px solid #e2e4e8;"
            " border-radius:8px; }"
        )
        self.results.itemDoubleClicked.connect(lambda _item: self.start_add())
        self.results.currentItemChanged.connect(lambda *_: self._selection_changed())
        root.addWidget(self.results)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)

        self.status = QLabel("Press Scan to look for bulbs in pairing mode.")
        self.status.setObjectName("subtitle")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setPlaceholderText("Progress messages appear here")
        root.addWidget(self.log)

        buttons = QDialogButtonBox()
        self.add_button = buttons.addButton(
            "Add selected bulb", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.add_button.setObjectName("primary")
        self.add_button.setEnabled(False)
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.add_button.clicked.connect(self.start_add)
        self.cancel_button.clicked.connect(self.reject)
        root.addWidget(buttons)

        worker.scan_ready.connect(self.on_scan)
        worker.added.connect(self.on_added)
        worker.failed.connect(self.on_failed)
        worker.log_line.connect(self.append_log)
        self.finished.connect(self._cleanup)

    # -- actions ---------------------------------------------------------
    def start_scan(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Scanning… keep the bulb close to the computer.")
        self.results.clear()
        self.add_button.setEnabled(False)
        self.worker.scan_new()

    def start_add(self) -> None:
        item = self.results.currentItem()
        if item is None or self._busy:
            return
        name = self.name_edit.text().strip() or f"Bulb {len(self.worker.state.devices) + 1}"
        self._set_busy(True, f"Adding {name!r}. Do not power the bulb off…")
        self.worker.add_bulb(item.data(Qt.ItemDataRole.UserRole), name)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.scan_button.setEnabled(not busy)
        self.add_button.setEnabled(not busy and self.results.currentItem() is not None)
        self.name_edit.setEnabled(not busy)
        self.results.setEnabled(not busy)
        self.cancel_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        if message:
            self.status.setText(message)

    def _selection_changed(self) -> None:
        if not self._busy:
            self.add_button.setEnabled(self.results.currentItem() is not None)

    # -- worker signals --------------------------------------------------
    def on_scan(self, found: list) -> None:
        self._set_busy(False)
        self.results.clear()
        for dev in found:
            label = dev["address"]
            if dev.get("name"):
                label += f"  ({dev['name']})"
            label += f"\nUUID {dev['uuid']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, dev["uuid"])
            item.setSizeHint(QSize(0, 46))
            self.results.addItem(item)
        if found:
            self.results.setCurrentRow(0)
            self.add_button.setEnabled(True)
            self.status.setText(
                f"Found {len(found)} unprovisioned bulb(s). "
                "Check the name, then press Add."
            )
        else:
            self.status.setText(
                "No unprovisioned bulbs found. Factory-reset the bulb (5× off/on), "
                "keep it close, and scan again."
            )

    def on_added(self, unicast: int, name: str) -> None:
        self.unicast = unicast
        self._set_busy(False)
        self.accept()

    def on_failed(self, message: str) -> None:
        if not self._busy:
            return
        self._set_busy(False, f"Failed: {message}")
        QMessageBox.critical(
            self, "Could not add the bulb",
            f"{message}\n\nIf the bulb is still unprovisioned, factory-reset "
            "it and try again.",
        )

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _cleanup(self, _result: int) -> None:
        for signal, slot in (
            (self.worker.scan_ready, self.on_scan),
            (self.worker.added, self.on_added),
            (self.worker.failed, self.on_failed),
            (self.worker.log_line, self.append_log),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            self.status.setText("Please wait for the current step to finish…")
            return
        super().closeEvent(event)


# ----------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    def __init__(self, worker: MeshWorker) -> None:
        super().__init__()
        self.worker = worker
        self.selected: int | None = None
        self._shown: int | None = None
        self.devices: dict[int, dict] = {}
        self.online: dict[int, bool] = {}
        self.states: dict[int, dict] = {}
        self.rows: dict[int, BulbRow] = {}
        self._updating = False
        self._busy = False
        self._queued: tuple[str, object] | None = None
        self._last_level = 0
        self._range_max = 50
        self._last_hue: int | None = None
        self._last_sat: int | None = None
        # True while the temperature slider shows an estimated white point for
        # a colour rather than the lamp's actual CTL temperature.
        self._temp_estimate = False

        self.setWindowTitle("Mesh Bulbs")
        self.resize(980, 720)
        self.setMinimumSize(820, 600)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.build_sidebar())
        layout.addWidget(self.build_content(), 1)

        self.level_timer = QTimer(self)
        self.level_timer.setSingleShot(True)
        self.level_timer.setInterval(350)
        self.level_timer.timeout.connect(self._send_level)
        self.temp_timer = QTimer(self)
        self.temp_timer.setSingleShot(True)
        self.temp_timer.setInterval(350)
        self.temp_timer.timeout.connect(self._send_temperature)
        self.poll_timer = QTimer(self)
        # A 10 s read doubles as a keepalive: the lamp's GATT proxy session
        # goes stale after ~20 s of silence even though the lamp stays powered.
        self.poll_timer.setInterval(10_000)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start()

        worker.devices_changed.connect(self.on_devices)
        worker.status_ready.connect(self.on_status)
        worker.failed.connect(self.on_failed)
        worker.log_line.connect(self.append_log)
        worker.config_exported.connect(self.on_config_exported)
        worker.config_imported.connect(self.on_config_imported)
        worker.config_error.connect(self.on_config_error)
        worker.controller_changed.connect(self.on_controller_changed)

        self.statusBar().showMessage("Ready")
        worker.refresh_devices()

    # -- layout ----------------------------------------------------------
    def build_sidebar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("sidebar")
        frame.setFixedWidth(280)
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 18, 16, 16)
        box.setSpacing(10)

        heading = QLabel("My bulbs")
        heading.setObjectName("title")
        box.addWidget(heading)

        self.bulb_list = QListWidget()
        self.bulb_list.setIconSize(QSize(12, 12))
        self.bulb_list.currentItemChanged.connect(self.on_selection)
        box.addWidget(self.bulb_list, 1)

        add = QPushButton("＋  Add bulb")
        add.setObjectName("primary")
        add.clicked.connect(self.open_add_dialog)
        box.addWidget(add)

        row = QHBoxLayout()
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self.rename_selected)
        self.remove_button = QPushButton("Forget")
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.rename_button)
        row.addWidget(self.remove_button)
        box.addLayout(row)

        config_row = QHBoxLayout()
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip(
            "Save the mesh keys and bulbs to a file for another computer"
        )
        export_menu = QMenu(self.export_button)
        export_menu.addAction(
            "Whole mesh…", lambda: self.export_config("mesh")
        )
        self.export_bulb_action = export_menu.addAction(
            "Selected bulb…", lambda: self.export_config("bulb")
        )
        self.export_button.setMenu(export_menu)
        self.import_button = QPushButton("Import…")
        self.import_button.setToolTip(
            "Load mesh keys and bulbs exported by another computer"
        )
        self.import_button.clicked.connect(self.import_config)
        config_row.addWidget(self.export_button)
        config_row.addWidget(self.import_button)
        box.addLayout(config_row)

        self.controller_button = QPushButton()
        self.controller_button.setToolTip(
            "The mesh unicast address this computer transmits from. Give each "
            "computer its own address when sharing the same bulbs."
        )
        self.controller_button.clicked.connect(self.change_controller_address)
        box.addWidget(self.controller_button)
        self._update_controller_label()

        self.sidebar_hint = QLabel("No bulbs yet. Add one to begin.")
        self.sidebar_hint.setObjectName("subtitle")
        self.sidebar_hint.setWordWrap(True)
        box.addWidget(self.sidebar_hint)
        return frame

    def build_content(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        box = QVBoxLayout(page)
        box.setContentsMargins(28, 24, 28, 24)
        box.setSpacing(16)

        self.name_label = QLabel("No bulb selected")
        self.name_label.setObjectName("title")
        self.meta_label = QLabel("Add a bulb or choose one from the list.")
        self.meta_label.setObjectName("subtitle")
        box.addWidget(self.name_label)
        box.addWidget(self.meta_label)

        power_card = QFrame()
        power_card.setObjectName("card")
        power_layout = QVBoxLayout(power_card)
        power_layout.setContentsMargins(18, 16, 18, 16)
        self.power_button = QPushButton("Turn on")
        self.power_button.setObjectName("power")
        self.power_button.setCheckable(True)
        self.power_button.setMinimumHeight(46)
        self.power_button.clicked.connect(self.toggle_power)
        power_layout.addWidget(self.power_button)
        box.addWidget(power_card)

        box.addWidget(self._section("Brightness"))
        level_row = QHBoxLayout()
        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.level_slider.setRange(0, 100)
        self.level_slider.setValue(0)
        self.level_slider.setEnabled(False)
        self.level_slider.valueChanged.connect(self.on_level_changed)
        self.level_value = QLabel("—")
        self.level_value.setFixedWidth(52)
        self.level_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        level_row.addWidget(self.level_slider, 1)
        level_row.addWidget(self.level_value)
        box.addLayout(level_row)

        box.addWidget(self._section("Colour temperature"))
        temp_row = QHBoxLayout()
        self.temp_slider = QSlider(Qt.Orientation.Horizontal)
        self.temp_slider.setRange(1000, 12000)
        self.temp_slider.setValue(2700)
        self.temp_slider.setEnabled(False)
        self.temp_slider.valueChanged.connect(self.on_temp_changed)
        self.temp_value = QLabel("—")
        self.temp_value.setFixedWidth(88)
        self.temp_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        temp_row.addWidget(self.temp_slider, 1)
        temp_row.addWidget(self.temp_value)
        box.addLayout(temp_row)
        self.temp_hint = QLabel("Warm ← → cool")
        self.temp_hint.setObjectName("subtitle")
        box.addWidget(self.temp_hint)

        colour_header = QHBoxLayout()
        colour_header.addWidget(self._section("Colour"))
        colour_header.addStretch(1)
        self.colour_preview = QLabel()
        self.colour_preview.setFixedSize(34, 24)
        self.colour_preview.setStyleSheet(
            "background:#dddddd; border:1px solid #cfd3d8; border-radius:6px;"
        )
        colour_header.addWidget(self.colour_preview)
        box.addLayout(colour_header)

        self.swatch_grid = QGridLayout()
        self.swatch_grid.setSpacing(8)
        self.swatch_buttons: list[QPushButton] = []
        for index, (name, hex_colour, kelvin) in enumerate(PRESETS):
            button = self._swatch(name, hex_colour, kelvin)
            self.swatch_grid.addWidget(button, index // 5, index % 5)
            self.swatch_buttons.append(button)
        custom = QPushButton("Custom…")
        custom.setMinimumHeight(38)
        custom.clicked.connect(self.pick_colour)
        self.swatch_grid.addWidget(custom, len(PRESETS) // 5, len(PRESETS) % 5)
        self.swatch_buttons.append(custom)
        box.addLayout(self.swatch_grid)

        box.addWidget(self._section("Activity"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("Mesh activity and errors appear here")
        box.addWidget(self.log)
        box.addStretch(1)
        return scroll

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _swatch(
        self, name: str, hex_colour: str, kelvin: int | None = None
    ) -> QPushButton:
        colour = QColor(hex_colour)
        button = QPushButton(name)
        button.setMinimumHeight(38)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            f"QPushButton {{ background:{hex_colour}; color:{readable_text(colour)};"
            " border:1px solid #c9ced6; border-radius:8px; padding:6px; }"
            "QPushButton:hover { border:2px solid #2f6fed; }"
            "QPushButton:disabled { background:#eceef1; color:#9aa0a6; }"
        )
        if kelvin is None:
            button.clicked.connect(
                lambda _=False, h=hex_colour: self.set_colour(h)
            )
        else:
            button.clicked.connect(
                lambda _=False, h=hex_colour, k=kelvin: self.set_temperature(k, h)
            )
        return button

    # -- collection ------------------------------------------------------
    def on_devices(self, devices: list) -> None:
        previous = self.selected
        self.devices = {d["unicast"]: d for d in devices}
        self.bulb_list.blockSignals(True)
        self.bulb_list.clear()
        self.rows.clear()
        for dev in devices:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dev["unicast"])
            item.setSizeHint(QSize(0, 54))
            self.bulb_list.addItem(item)
            row = BulbRow()
            self.bulb_list.setItemWidget(item, row)
            self.rows[dev["unicast"]] = row
        self._refresh_item_texts()
        if devices:
            index = next(
                (i for i, d in enumerate(devices) if d["unicast"] == previous), 0
            )
            self.bulb_list.setCurrentRow(index)
        self.bulb_list.blockSignals(False)

        if self.selected in self.devices:
            self.name_label.setText(self.devices[self.selected].get("name", "Bulb"))
            self._show_stored_colour(self.devices[self.selected])

        if devices:
            self.sidebar_hint.setText(
                f"{len(devices)} bulb{'s' if len(devices) != 1 else ''} in this mesh."
            )
        else:
            self.selected = None
            self._shown = None
            self.show_empty()
            self.sidebar_hint.setText("No bulbs yet. Add one to begin.")
        self.rename_button.setEnabled(bool(devices))
        self.remove_button.setEnabled(bool(devices))

        # The list was rebuilt with signals blocked, so the selection signal did
        # not fire.  Select/refresh explicitly: this connects on startup and
        # after a bulb is added, while on_selection's early return keeps routine
        # status updates from re-triggering a refresh loop.
        item = self.bulb_list.currentItem()
        if item is not None:
            self.on_selection(item, None)
        self._update_config_actions()

    def _update_config_actions(self) -> None:
        self.export_bulb_action.setEnabled(
            self.selected is not None and self.selected in self.devices
        )

    def _update_controller_label(self) -> None:
        address = int(self.worker.state.data.get("provisioner_addr", 0x0001))
        self.controller_button.setText(f"Controller 0x{address:04x}…")

    def _dot_colour(self, unicast: int) -> str:
        if not self.online.get(unicast):
            return "#9aa0a6"
        state = self.states.get(unicast, {})
        return "#2fb344" if state.get("on") else "#f0a020"

    def _refresh_item_texts(self) -> None:
        for row_index in range(self.bulb_list.count()):
            item = self.bulb_list.item(row_index)
            unicast = item.data(Qt.ItemDataRole.UserRole)
            dev = self.devices.get(unicast, {})
            widget = self.rows.get(unicast)
            if widget is None:
                continue
            if self.online.get(unicast):
                state = self.states.get(unicast, {})
                status = "on" if state.get("on") else "off"
                status += f" · {state.get('level_pct', 0)}%"
            else:
                status = "not connected"
            widget.update_row(
                dev.get("name", "Bulb"),
                f"0x{unicast:04x} · {status}",
                self._dot_colour(unicast),
            )

    def on_selection(self, current, _previous) -> None:
        if current is None:
            self.selected = None
            self._shown = None
            self.show_empty()
            self._update_config_actions()
            return
        unicast = current.data(Qt.ItemDataRole.UserRole)
        if unicast == self._shown:
            return
        self._queued = None
        self.selected = unicast
        self._shown = unicast
        self._update_config_actions()
        dev = self.devices.get(unicast, {})
        self.name_label.setText(dev.get("name", "Bulb"))
        self.meta_label.setText(
            f"0x{unicast:04x}  ·  {dev.get('address', '')}  ·  connecting…"
        )
        self.set_controls_enabled(False)
        self._show_stored_colour(dev)
        self.worker.refresh_status(unicast)
        self.statusBar().showMessage(f"Connecting to {dev.get('name', 'bulb')}…")

    def show_empty(self) -> None:
        self.name_label.setText("No bulb selected")
        self.meta_label.setText("Add a bulb or choose one from the list.")
        self.set_controls_enabled(False)
        if hasattr(self, "export_bulb_action"):
            self.export_bulb_action.setEnabled(False)

    # -- state updates ---------------------------------------------------
    def on_status(self, unicast: int, st: dict) -> None:
        self.online[unicast] = True
        self.states[unicast] = st
        self._refresh_item_texts()
        if unicast != self.selected:
            return
        self._busy = False
        self._updating = True
        try:
            level = int(st.get("level_pct", 0))
            mn, mx = st.get("range", (1, 50))
            self._range_max = mx
            self._last_level = st.get("level", 0) or 0
            self.level_slider.setValue(level)
            self.level_value.setText(f"{level}%")
            self.level_slider.setEnabled(True)

            on = bool(st.get("on"))
            self.power_button.setChecked(on)
            self.power_button.setText("Turn off" if on else "Turn on")

            dev = self.devices.get(unicast, {})
            self.set_controls_enabled(True)
            if not dev.get("has_colour", True):
                for button in self.swatch_buttons:
                    button.setEnabled(False)

            temperature = st.get("temperature")
            temp_range = st.get("temperature_range")
            if temp_range:
                self.temp_slider.setRange(*temp_range)
            has_temperature = dev.get("has_temperature", True)
            mode = st.get("mode")
            hue, sat = st.get("hue"), st.get("saturation")
            if self.temp_timer.isActive():
                # A drag is about to send its own value; a poll that arrives in
                # the meantime must not snap the slider back to the old state.
                pass
            elif (
                mode == "colour"
                and hue is not None
                and sat is not None
                and has_temperature
            ):
                # The lamp is emitting colour, so there is no meaningful CTL
                # value.  Point the slider at the closest white point so it
                # reflects the colour instead of a stale temperature.
                colour = QColor.fromHsl(
                    int(hue) % 360, int(sat) * 255 // 100, 150
                )
                self._set_temp_display(colour_temperature(colour), estimate=True)
            elif temperature is not None and has_temperature:
                self._set_temp_display(int(temperature), estimate=False)
            else:
                self._temp_estimate = False
                self.temp_value.setText("—")
                self.temp_slider.setEnabled(False)
                self.temp_hint.setText("Warm ← → cool")

            name = dev.get("name", "Bulb")
            self.name_label.setText(name)
            self.power_button.setEnabled(True)
            self.meta_label.setText(
                f"0x{unicast:04x}  ·  {dev.get('address', '')}  ·  "
                f"{st.get('level', 0)}/{mx}"
            )
            self._show_stored_colour(st)
            self.statusBar().showMessage(f"{name} · connected", 3000)
        finally:
            self._updating = False
        self._flush_queue()

    def on_failed(self, message: str) -> None:
        self.append_log(f"error: {message}")
        self._busy = False
        if self.selected is not None:
            self.online[self.selected] = False
            self._refresh_item_texts()
            self.meta_label.setText(
                "Not connected — click a control to retry, or wait for the next poll."
            )
            # Keep the controls usable; the next action creates a fresh link.
            self.set_controls_enabled(True)
        self.statusBar().showMessage(f"Error: {message}", 6000)
        self._flush_queue()

    def append_log(self, line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"{stamp}  {line}")
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # -- controls --------------------------------------------------------
    def set_controls_enabled(self, enabled: bool) -> None:
        self.power_button.setEnabled(enabled)
        if not enabled:
            self.level_slider.setEnabled(False)
            self.temp_slider.setEnabled(False)
        for button in self.swatch_buttons:
            button.setEnabled(enabled)

    def _show_stored_colour(self, source: dict) -> None:
        mode = source.get("mode")
        hue, sat = source.get("hue"), source.get("saturation")
        if mode == "colour" and hue is not None and sat is not None:
            self._last_hue, self._last_sat = int(hue), int(sat)
            colour = QColor.fromHsl(int(hue) % 360, int(sat) * 255 // 100, 150)
        elif source.get("temperature"):
            colour = temperature_colour(int(source["temperature"]))
        else:
            colour = QColor("#dddddd")
        self.colour_preview.setStyleSheet(
            f"background:{colour.name()}; border:1px solid #cfd3d8; border-radius:6px;"
        )

    def _send(self, action: str, value) -> None:
        if self.selected is None:
            return
        if self._busy:
            self._queued = (action, value)
            return
        self._busy = True
        self.worker.control(self.selected, action, value)

    def _flush_queue(self) -> None:
        if self._queued and not self._busy:
            action, value = self._queued
            self._queued = None
            self._send(action, value)

    def toggle_power(self) -> None:
        if self._updating:
            return
        self._send("power", self.power_button.isChecked())

    def on_level_changed(self, value: int) -> None:
        self.level_value.setText(f"{value}%")
        if self._updating:
            return
        self.level_timer.start()

    def _send_level(self) -> None:
        value = self.level_slider.value()
        if value == round(self._last_level * 100 / self._range_max):
            return
        self._send("level", value)

    def on_temp_changed(self, value: int) -> None:
        if self._updating:
            # A refresh from the worker owns the label and the hint text.
            return
        if self._temp_estimate:
            # Dragging the slider takes the lamp out of colour mode, so the
            # estimate is no longer relevant.
            self._temp_estimate = False
            self.temp_hint.setText("Warm ← → cool")
        self.temp_value.setText(f"{value} K")
        self.temp_timer.start()

    def _set_temp_display(self, kelvin: int, *, estimate: bool) -> None:
        """Move the slider without emitting a control command."""
        low, high = self.temp_slider.minimum(), self.temp_slider.maximum()
        kelvin = max(low, min(high, int(kelvin)))
        previous = self._updating
        self._updating = True
        try:
            self.temp_slider.setValue(kelvin)
        finally:
            self._updating = previous
        self._temp_estimate = estimate
        self.temp_value.setText(f"≈ {kelvin} K" if estimate else f"{kelvin} K")
        self.temp_hint.setText(
            "Estimated white point for this colour — drag to switch to white"
            if estimate
            else "Warm ← → cool"
        )
        self.temp_slider.setEnabled(True)

    def _send_temperature(self) -> None:
        self._send("temperature", self.temp_slider.value())

    def set_temperature(self, kelvin: int, hex_colour: str | None = None) -> None:
        if self.selected is None:
            return
        kelvin = int(kelvin)
        if hex_colour is not None:
            self.colour_preview.setStyleSheet(
                f"background:{hex_colour}; border:1px solid #cfd3d8; border-radius:6px;"
            )
        self._set_temp_display(kelvin, estimate=False)
        self._send("temperature", kelvin)

    def set_colour(self, hex_colour: str) -> None:
        colour = QColor(hex_colour)
        hue = colour.hue() if colour.hue() >= 0 else 0
        sat = colour.saturation() * 100 // 255
        self._last_hue, self._last_sat = hue, sat
        self.colour_preview.setStyleSheet(
            f"background:{hex_colour}; border:1px solid #cfd3d8; border-radius:6px;"
        )
        dev = self.devices.get(self.selected, {}) if self.selected else {}
        if dev.get("has_temperature", True):
            # Move the slider immediately; the status read back from the lamp
            # will confirm the same estimate.
            self._set_temp_display(colour_temperature(colour), estimate=True)
        self._send("colour", hex_colour)

    def pick_colour(self) -> None:
        initial = QColor.fromHsl(
            (self._last_hue or 0),
            (self._last_sat or 100) * 255 // 100,
            150,
        )
        colour = QColorDialog.getColor(initial, self, "Choose a colour")
        if colour.isValid():
            self.set_colour(colour.name().lstrip("#"))

    # -- configuration files --------------------------------------------
    def export_config(self, kind: str) -> None:
        if kind == "bulb":
            if self.selected is None or self.selected not in self.devices:
                QMessageBox.information(
                    self, "Export bulb", "Choose a bulb first."
                )
                return
            dev = self.devices[self.selected]
            safe = "".join(
                char if char.isalnum() or char in "-_" else "-"
                for char in dev.get("name", "bulb")
            ).strip("-_")
            default = f"{safe or 'bulb'}-bulb.json"
            title = "Export selected bulb"
        else:
            default = f"mesh-bulbs-{datetime.now():%Y%m%d}.json"
            title = "Export whole mesh"

        path, _ = QFileDialog.getSaveFileName(
            self,
            title,
            str(Path.home() / default),
            "Configuration files (*.json);;All files (*)",
        )
        if not path:
            return
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".json")
        self.statusBar().showMessage(f"Exporting to {target}…")
        self.worker.export_config(
            str(target),
            kind=kind,
            unicast=self.selected if kind == "bulb" else None,
        )

    def import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import configuration",
            str(Path.home()),
            "Configuration files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            package = meshcore.read_config(path)
        except meshcore.MeshCoreError as exc:
            self.on_config_error(str(exc))
            return

        description = meshcore.describe_config(package)
        same = meshcore.same_mesh(self.worker.state, package)
        existing = len(self.worker.state.devices)
        replace = False

        if existing and not same:
            answer = QMessageBox.warning(
                self,
                "Different mesh",
                f"{description}\n\n"
                f"This computer already has {existing} bulb(s) on a different "
                "mesh. Importing will replace its mesh keys and bulb list.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            replace = True
        elif existing:
            box = QMessageBox(self)
            box.setWindowTitle("Import configuration")
            box.setText(f"Import {description}?")
            box.setInformativeText(
                "Merge keeps your other bulbs and controller address, and "
                "updates matching ones. Replace uses only the bulbs in this "
                "file and copies its controller address."
            )
            merge = box.addButton("Merge", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(merge)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None:
                return
            role = box.buttonRole(clicked)
            if role == QMessageBox.ButtonRole.DestructiveRole:
                replace = True
            elif role != QMessageBox.ButtonRole.AcceptRole:
                return
        else:
            answer = QMessageBox.question(
                self,
                "Import configuration",
                f"Import {description} into this empty setup?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.statusBar().showMessage("Importing configuration…")
        self.worker.import_config(package, replace=replace)

    def on_config_exported(self, path: str, summary: str) -> None:
        self.append_log(summary)
        self.statusBar().showMessage("Configuration exported", 5000)
        QMessageBox.information(
            self,
            "Configuration exported",
            f"{summary}\n\n"
            "This file contains the mesh keys. Keep it private and transfer it "
            "securely.\n\n"
            "Never run this computer and the other one at the same time.",
        )

    def on_config_imported(self, summary: str) -> None:
        self.append_log(summary)
        self._update_controller_label()
        self.online.clear()
        self.states.clear()
        self.selected = None
        self._shown = None
        self.worker.refresh_devices()
        self.statusBar().showMessage("Configuration imported", 5000)
        QMessageBox.information(
            self,
            "Configuration imported",
            f"{summary}\n\n"
            "Use this computer only while the other controller is closed.\n\n"
            "If this is a second computer, click the Controller button and "
            "give it a free address such as 0x7fff.",
        )

    def on_config_error(self, message: str) -> None:
        self.append_log(f"configuration: {message}")
        self.statusBar().showMessage("Configuration error", 6000)
        QMessageBox.critical(self, "Configuration error", message)

    # -- controller identity --------------------------------------------
    def change_controller_address(self) -> None:
        current = int(self.worker.state.data.get("provisioner_addr", 0x0001))
        text, ok = QInputDialog.getText(
            self,
            "Controller address",
            "Mesh unicast address this computer uses:\n\n"
            "Give each computer its own address when they share the same "
            "bulbs (for example 0x7fff on the second one). Only one "
            "controller should run at a time.",
            text=f"0x{current:04x}",
        )
        if not ok:
            return
        try:
            address = meshcore.parse_unicast(text)
        except meshcore.MeshCoreError as exc:
            QMessageBox.critical(self, "Controller address", str(exc))
            return
        if address == current:
            return
        answer = QMessageBox.question(
            self,
            "Change controller address",
            f"Use 0x{address:04x} as this computer's controller address?\n\n"
            "No bulb is re-provisioned. This only changes the source address "
            "and replay-sequence space this computer uses.\n\n"
            "Keep the other controller closed while you make the change.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.statusBar().showMessage("Changing controller address…")
        self.worker.set_controller_address(address)

    def on_controller_changed(self, address: int, summary: str) -> None:
        self.append_log(summary)
        self._update_controller_label()
        self.online.clear()
        self.states.clear()
        self._refresh_item_texts()
        if self.selected is not None:
            self.worker.refresh_status(self.selected)
        self.statusBar().showMessage(f"Controller address 0x{address:04x}", 5000)
        QMessageBox.information(
            self,
            "Controller address",
            f"{summary}\n\n"
            "Each computer now has its own replay/sequence history. Use only "
            "one controller at a time, and merge configuration files when "
            "bulbs are added or renamed.",
        )

    # -- collection actions ---------------------------------------------
    def open_add_dialog(self) -> None:
        self.poll_timer.stop()
        dialog = AddBulbDialog(self.worker, len(self.devices) + 1, self)
        result = dialog.exec()
        self.poll_timer.start()
        if result == QDialog.DialogCode.Accepted and dialog.unicast is not None:
            self.selected = dialog.unicast
            self.worker.refresh_devices()
            self.statusBar().showMessage("Bulb added", 4000)
        elif self.selected is not None:
            # Scanning closed the old proxy session; reconnect promptly.
            self.worker.refresh_status(self.selected)

    def rename_selected(self) -> None:
        if self.selected is None:
            return
        dev = self.devices.get(self.selected, {})
        name, ok = QInputDialog.getText(
            self, "Rename bulb", "Name:", text=dev.get("name", "Bulb")
        )
        if ok and name.strip():
            self.worker.rename(self.selected, name.strip())

    def remove_selected(self) -> None:
        if self.selected is None:
            return
        dev = self.devices.get(self.selected, {})
        answer = QMessageBox.warning(
            self,
            "Forget this bulb?",
            f"Forget {dev.get('name', 'this bulb')!r}?\n\n"
            "Its device key is deleted from this computer. The bulb stays on "
            "this mesh, but you will not be able to control it again until it "
            "is factory-reset and added back.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.online.pop(self.selected, None)
            self.states.pop(self.selected, None)
            self.worker.remove(self.selected)
            self.selected = None
            self._queued = None
            self.statusBar().showMessage("Bulb forgotten", 4000)

    def _poll(self) -> None:
        if self.selected is not None and not self._busy:
            self.worker.refresh_status(self.selected)

    def closeEvent(self, event) -> None:
        self.poll_timer.stop()
        self.worker.shutdown()
        super().closeEvent(event)
        # Hidden Qt helper windows can keep the event loop alive after the main
        # window closes; quit explicitly so the state lock is always released.
        app = QApplication.instance()
        if app is not None:
            app.quit()


# ----------------------------------------------------------------------- main


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname).1s %(name)s: %(message)s"
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Mesh Bulbs")
    app.setDesktopFileName("bulbgui")
    app.setStyleSheet(STYLE)
    icon_path = Path(__file__).with_name("icon.ico" if os.name == "nt" else "icon.svg")
    if not icon_path.exists():
        icon_path = Path(__file__).with_name("icon.svg")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    try:
        worker = MeshWorker(str(meshcore.DEFAULT_STATE))
    except meshcore.MeshCoreError as exc:
        QMessageBox.critical(None, "Mesh Bulbs", str(exc))
        return 1
    worker.start()

    window = MainWindow(worker)
    window.show()
    app.aboutToQuit.connect(worker.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
