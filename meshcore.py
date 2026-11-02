"""meshcore — shared state, discovery, provisioning and a persistent lamp link.

Used by both the CLI (`bulbctl.py`) and the desktop GUI (`bulbgui.py`).

Everything runs as the normal user: BLE goes through `bleak` to the BlueZ
daemon. Mesh crypto/provisioning comes from the `btmesh` library (MIT) and its
phase0 harness, both vendored next to this file.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "phase0"))

import provision_and_toggle as p0  # noqa: E402

from btmesh.access import (  # noqa: E402
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
    OP_CONFIG_APPKEY_STATUS,
    OP_CONFIG_COMPOSITION_DATA_STATUS,
    OP_CONFIG_MODEL_APP_STATUS,
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_CTL_STATUS,
    OP_LIGHT_CTL_TEMPERATURE_RANGE_GET,
    OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS,
    OP_LIGHT_LIGHTNESS_STATUS,
    config_appkey_add,
    config_composition_data_get,
    config_model_app_bind,
    encode_opcode,
    generic_onoff_get,
    generic_onoff_set,
    light_ctl_get,
    light_ctl_set,
    light_lightness_get,
    light_lightness_set,
    parse_composition_data_status,
    parse_config_appkey_status,
    parse_config_model_app_status,
    parse_generic_onoff_status,
    parse_light_ctl_status,
    parse_light_lightness_status,
)
from btmesh.bearer import (  # noqa: E402
    proxy_candidates_from_discoveries,
    unprovisioned_from_discoveries,
)
from btmesh.crypto import k3  # noqa: E402
from btmesh.node import MeshNode  # noqa: E402
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU  # noqa: E402
from btmesh.pump import BearerPump  # noqa: E402

log = logging.getLogger("meshcore")


def _default_state_path() -> Path:
    """Per-user state location, with the platform's conventional directory."""
    if os.name == "nt":
        # %LOCALAPPDATA% is per-machine and inherited user-only ACLs apply.
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "bulbctl" / "state.json"
        return Path.home() / "AppData" / "Local" / "bulbctl" / "state.json"
    return Path.home() / ".config" / "bulbctl" / "state.json"


DEFAULT_STATE = _default_state_path()


def _protect(path: Path, mode: int) -> None:
    """Restrict POSIX permissions; Windows uses the profile's NTFS ACLs."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _lock_fd(fd: int) -> None:
    """Take a non-blocking exclusive lock on the first byte of ``fd``."""
    if os.name == "nt":
        # msvcrt locks byte ranges, so ensure the file has one byte and lock
        # from offset zero. Windows releases the lock when the handle closes.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)

MODEL_LIGHT_LIGHTNESS_SETUP_SERVER = 0x1301
MODEL_LIGHT_HSL_SERVER = 0x1307

CONTROLLABLE_MODELS = {
    0x1000: "Generic OnOff Server",
    0x1300: "Light Lightness Server",
    0x1301: "Light Lightness Setup Server",
    0x1303: "Light CTL Server",
    0x1304: "Light CTL Setup Server",
    0x1306: "Light CTL Temperature Server",
    0x1307: "Light HSL Server",
    0x1308: "Light HSL Setup Server",
    0x130A: "Light HSL Hue Server",
    0x130B: "Light HSL Saturation Server",
    0x130C: "Light xyL Server",
}

OP_LIGHT_HSL_GET = 0x826D
OP_LIGHT_HSL_SET = 0x8276
OP_LIGHT_HSL_STATUS = 0x8278
OP_LIGHT_LIGHTNESS_RANGE_GET = 0x8257
OP_LIGHT_LIGHTNESS_RANGE_STATUS = 0x8258
OP_LIGHT_LIGHTNESS_RANGE_SET = 0x825B


class MeshCoreError(Exception):
    pass


# --------------------------------------------------------------------- state


class State:
    """The mesh state file: keys, SEQ cursor and the bulb collection.

    A second process using the same file would reuse SEQ values and overwrite
    the other's changes, so construction takes a non-blocking advisory lock.
    """

    # If the previous process did not shut down cleanly, the cursor it saved can
    # be behind the highest SEQ the nodes have already accepted from us. Every
    # later message would then be discarded as a replay, which looks exactly
    # like a dead lamp. A run marker detects that case and jumps the cursor
    # forward, the same way changing the controller address does.
    CRASH_SEQ_MARGIN = 10_000

    def __init__(self, path: Path | str = DEFAULT_STATE, *, lock: bool = True) -> None:
        self.path = Path(path)
        self._lock_fd: int | None = None
        self._marker: Path | None = None
        if lock:
            self._marker = self.path.with_suffix(self.path.suffix + ".running")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _protect(self.path.parent, 0o700)
        if lock:
            self._acquire_lock()
        if not self.path.exists():
            self.data = {
                "netkey": secrets.token_bytes(16).hex(),
                "appkey": secrets.token_bytes(16).hex(),
                "iv_index": 0,
                "netkey_index": 0,
                "appkey_index": 0,
                "provisioner_addr": 0x0001,
                "next_unicast": 0x0002,
                "seq": 0,
                "devices": {},
            }
            self.save()
            log.info("created fresh mesh state %s", self.path)
        else:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self._migrate()
            _protect(self.path, 0o600)
            if self._marker is not None and self._marker.exists():
                old = int(self.data.get("seq", 0))
                self.data["seq"] = min(0xFFFFFF, old + self.CRASH_SEQ_MARGIN)
                self.save()
                log.warning(
                    "previous run ended uncleanly; advanced SEQ %d -> %d "
                    "to avoid replay rejection",
                    old,
                    self.data["seq"],
                )
        if self._marker is not None:
            try:
                self._marker.write_text(f"{os.getpid()}\n", encoding="ascii")
                _protect(self._marker, 0o600)
            except OSError:
                self._marker = None

    def _acquire_lock(self) -> None:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(lock_path, flags, 0o600)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            raise MeshCoreError(
                "another Mesh Bulbs or bulbctl process is already using "
                f"{self.path} — close it first"
            ) from None
        self._lock_fd = fd

    def release(self) -> None:
        # A clean close removes the run marker; a crash leaves it behind so the
        # next open can advance SEQ past anything the nodes have already seen.
        if self._marker is not None:
            try:
                self._marker.unlink()
            except OSError:
                pass
            self._marker = None
        if self._lock_fd is None:
            return
        try:
            _unlock_fd(self._lock_fd)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass

    def _migrate(self) -> None:
        """Bring pre-GUI state files (single bulb, global bound_models) forward."""
        legacy_models = self.data.pop("bound_models", None)
        changed = legacy_models is not None
        entries = list(self.data["devices"].items())
        for index, (_key, entry) in enumerate(entries, start=1):
            if "name" not in entry:
                entry["name"] = f"Bulb {index}"
                changed = True
            if "bound_models" not in entry:
                entry["bound_models"] = legacy_models or []
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        _protect(tmp, 0o600)
        # Windows antivirus/indexers can briefly hold the target open; retry
        # the atomic replace instead of failing a mesh command.
        for attempt in range(5):
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    # -- shortcuts
    @property
    def netkey(self) -> bytes:
        return bytes.fromhex(self.data["netkey"])

    @property
    def appkey(self) -> bytes:
        return bytes.fromhex(self.data["appkey"])

    @property
    def network_id(self) -> bytes:
        return k3(self.netkey)

    @property
    def devices(self) -> dict[str, dict]:
        return self.data["devices"]

    def device(self, unicast: int) -> dict:
        entry = self.devices.get(f"0x{unicast:04x}")
        if entry is None:
            raise MeshCoreError(f"no bulb at 0x{unicast:04x}")
        return entry

    def device_by_name(self, name: str) -> int:
        for key, entry in self.devices.items():
            if (entry.get("name") or "").lower() == name.lower():
                return int(key, 16)
        raise MeshCoreError(f"no bulb named {name!r}")

    def add_device(self, unicast: int, *, name: str, uuid: str, address: str,
                   device_key: bytes, bound_models: list[int]) -> None:
        self.devices[f"0x{unicast:04x}"] = {
            "name": name,
            "device_key": device_key.hex(),
            "uuid": uuid,
            "address": address,
            "bound_models": bound_models,
        }
        self.save()

    def rename(self, unicast: int, name: str) -> None:
        self.device(unicast)["name"] = name
        self.save()

    def remove(self, unicast: int) -> None:
        self.devices.pop(f"0x{unicast:04x}", None)
        self.save()

    def resolve(self, spec: str | None) -> int:
        """Accept a name, '0x0002', or None = the only/first bulb."""
        if not self.devices:
            raise MeshCoreError("no bulbs yet — add one first")
        if not spec:
            return int(next(iter(self.devices)), 16)
        try:
            addr = int(spec, 16) if spec.lower().startswith("0x") else None
        except ValueError:
            addr = None
        if addr is not None and f"0x{addr:04x}" in self.devices:
            return addr
        return self.device_by_name(spec)

    def snapshot(self) -> list[dict]:
        """A GUI-friendly copy of the collection."""
        out = []
        for key, entry in self.devices.items():
            models = entry.get("bound_models") or []
            out.append({
                "unicast": int(key, 16),
                "name": entry.get("name") or f"0x{int(key, 16):04x}",
                "address": entry.get("address", ""),
                "uuid": entry.get("uuid", ""),
                "models": models,
                "has_colour": 0x1307 in models,
                "has_temperature": 0x1303 in models or 0x1304 in models,
                "hue": entry.get("hue"),
                "saturation": entry.get("saturation"),
                "temperature": entry.get("temperature"),
                "mode": entry.get("mode"),
            })
        return out


# -------------------------------------------------------- configuration files

CONFIG_FORMAT = "bulbctl-config"
CONFIG_VERSION = 1

_CONFIG_MESH_FIELDS = (
    "netkey",
    "appkey",
    "iv_index",
    "netkey_index",
    "appkey_index",
    "provisioner_addr",
    "next_unicast",
    "seq",
)
# Fields that decide whether two files describe the same mesh.  The SEQ cursor
# and next unicast address move over time and are deliberately not compared.
_CONFIG_IDENTITY_FIELDS = (
    "netkey",
    "appkey",
    "iv_index",
    "netkey_index",
    "appkey_index",
)
_HEX_DIGITS = set("0123456789abcdefABCDEF")


def _config_int(value, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MeshCoreError(f"invalid configuration: {field} must be an integer")
    if not minimum <= value <= maximum:
        raise MeshCoreError(
            f"invalid configuration: {field} must be {minimum}..{maximum}"
        )
    return value


def _config_hex(value, field: str, size: int = 16) -> str:
    if not isinstance(value, str) or len(value) != size * 2:
        raise MeshCoreError(
            f"invalid configuration: {field} must be {size * 2} hex digits"
        )
    if any(char not in _HEX_DIGITS for char in value):
        raise MeshCoreError(f"invalid configuration: {field} is not hexadecimal")
    return value.lower()


def _normalise_mesh(raw) -> dict:
    if not isinstance(raw, dict):
        raise MeshCoreError("invalid configuration: mesh must be an object")
    return {
        "netkey": _config_hex(raw.get("netkey"), "mesh.netkey"),
        "appkey": _config_hex(raw.get("appkey"), "mesh.appkey"),
        "iv_index": _config_int(raw.get("iv_index"), "mesh.iv_index", 0, 0xFFFFFFFF),
        "netkey_index": _config_int(
            raw.get("netkey_index"), "mesh.netkey_index", 0, 0x0FFF
        ),
        "appkey_index": _config_int(
            raw.get("appkey_index"), "mesh.appkey_index", 0, 0x0FFF
        ),
        "provisioner_addr": _config_int(
            raw.get("provisioner_addr"), "mesh.provisioner_addr", 1, 0x7FFF
        ),
        "next_unicast": _config_int(
            raw.get("next_unicast"), "mesh.next_unicast", 1, 0x8000
        ),
        "seq": _config_int(raw.get("seq"), "mesh.seq", 0, 0xFFFFFF),
    }


def _normalise_device(key, raw) -> tuple[str, dict]:
    if not isinstance(key, str):
        raise MeshCoreError("invalid configuration: device keys must be strings")
    key = key.strip().lower()
    if len(key) != 6 or not key.startswith("0x"):
        raise MeshCoreError(
            f"invalid configuration: device address {key!r} must look like 0x0002"
        )
    try:
        unicast = int(key, 16)
    except ValueError as exc:
        raise MeshCoreError(
            f"invalid configuration: device address {key!r} is not hexadecimal"
        ) from exc
    unicast = _config_int(unicast, "device address", 1, 0x7FFF)
    key = f"0x{unicast:04x}"
    if not isinstance(raw, dict):
        raise MeshCoreError(f"invalid configuration: device {key} must be an object")

    entry = copy.deepcopy(raw)
    name = entry.get("name")
    if name is None:
        entry["name"] = key
    elif not isinstance(name, str):
        raise MeshCoreError(f"invalid configuration: {key}.name must be text")

    uuid = entry.get("uuid") or ""
    if uuid:
        entry["uuid"] = _config_hex(uuid, f"{key}.uuid")
    else:
        entry["uuid"] = ""

    address = entry.get("address") or ""
    if not isinstance(address, str):
        raise MeshCoreError(f"invalid configuration: {key}.address must be text")
    entry["address"] = address

    entry["device_key"] = _config_hex(entry.get("device_key"), f"{key}.device_key")

    models = entry.get("bound_models") or []
    if not isinstance(models, list):
        raise MeshCoreError(f"invalid configuration: {key}.bound_models must be a list")
    entry["bound_models"] = [
        _config_int(model, f"{key}.bound_models", 0, 0xFFFF) for model in models
    ]

    for field, minimum, maximum in (
        ("hue", 0, 360),
        ("saturation", 0, 100),
        ("temperature", 1, 0xFFFF),
        ("last_level", 0, 0xFFFF),
    ):
        value = entry.get(field)
        if value is not None:
            entry[field] = _config_int(value, f"{key}.{field}", minimum, maximum)
    mode = entry.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise MeshCoreError(f"invalid configuration: {key}.mode must be text")
    return key, entry


def _normalise_config(raw) -> dict:
    if not isinstance(raw, dict):
        raise MeshCoreError("invalid configuration: top level must be an object")
    if raw.get("format") != CONFIG_FORMAT:
        raise MeshCoreError(
            f"not a {CONFIG_FORMAT} file (missing or wrong 'format')"
        )
    version = raw.get("version")
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise MeshCoreError(
            f"unsupported configuration version {version!r}; expected {CONFIG_VERSION}"
        )
    kind = raw.get("kind")
    if kind not in ("mesh", "bulb"):
        raise MeshCoreError("invalid configuration: kind must be 'mesh' or 'bulb'")

    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, dict):
        raise MeshCoreError("invalid configuration: devices must be an object")
    devices: dict[str, dict] = {}
    for key, entry in devices_raw.items():
        key, entry = _normalise_device(key, entry)
        if key in devices:
            raise MeshCoreError(f"invalid configuration: duplicate address {key}")
        devices[key] = entry
    if kind == "bulb" and len(devices) != 1:
        raise MeshCoreError("invalid configuration: a bulb file must contain one bulb")
    if len(devices) > 0x7FFF:
        raise MeshCoreError("invalid configuration: too many bulbs")

    uuids: dict[str, str] = {}
    for key, entry in devices.items():
        uuid = entry.get("uuid")
        if not uuid:
            continue
        if uuid in uuids:
            raise MeshCoreError(
                f"invalid configuration: {key} and {uuids[uuid]} have the same UUID"
            )
        uuids[uuid] = key

    exported_at = raw.get("exported_at")
    return {
        "format": CONFIG_FORMAT,
        "version": CONFIG_VERSION,
        "kind": kind,
        "exported_at": exported_at if isinstance(exported_at, str) else "",
        "mesh": _normalise_mesh(raw.get("mesh")),
        "devices": devices,
    }


def read_config(path: Path | str) -> dict:
    """Read, validate and normalise an exported configuration file."""
    path = Path(path)
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise MeshCoreError(f"{path} is too large to be a configuration file")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeshCoreError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MeshCoreError(f"{path} is not valid JSON: {exc}") from exc
    return _normalise_config(raw)


def _mesh_from_state(data: dict) -> dict:
    return {field: data[field] for field in _CONFIG_MESH_FIELDS}


def _mesh_fingerprint(netkey: str) -> str:
    return hashlib.sha256(bytes.fromhex(netkey)).hexdigest()[:12]


def _same_mesh(current: dict, package_mesh: dict) -> bool:
    return all(
        current.get(field) == package_mesh.get(field)
        for field in _CONFIG_IDENTITY_FIELDS
    )


def same_mesh(state: State, package: dict) -> bool:
    """True when the package belongs to the state's existing mesh."""
    package = _normalise_config(package)
    return _same_mesh(state.data, package["mesh"])


def describe_config(package: dict) -> str:
    package = _normalise_config(package)
    fingerprint = _mesh_fingerprint(package["mesh"]["netkey"])
    if package["kind"] == "bulb":
        key, entry = next(iter(package["devices"].items()))
        name = entry.get("name") or key
        return f"bulb {name!r} at {key} (mesh {fingerprint})"
    names = [entry.get("name") or key for key, entry in package["devices"].items()]
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    count = len(names)
    label = f"{count} bulb" + ("" if count == 1 else "s")
    suffix = f" ({shown})" if shown else ""
    return f"whole mesh with {label}{suffix}, mesh {fingerprint}"


def _write_private_json(path: Path, data: dict) -> None:
    path = Path(path)
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True)
        _protect(parent, 0o700)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        _protect(tmp, 0o600)
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def export_config(
    state: State, path: Path | str, *, kind: str, unicast: int | None = None
) -> str:
    """Write all mesh keys, or one bulb plus its mesh keys, to ``path``."""
    if kind not in ("mesh", "bulb"):
        raise MeshCoreError("export kind must be 'mesh' or 'bulb'")
    if kind == "bulb":
        if unicast is None:
            raise MeshCoreError("choose a bulb to export")
        entry = state.device(unicast)
        devices = {f"0x{unicast:04x}": copy.deepcopy(entry)}
    else:
        devices = copy.deepcopy(state.devices)

    package = _normalise_config({
        "format": CONFIG_FORMAT,
        "version": CONFIG_VERSION,
        "kind": kind,
        "exported_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "mesh": _mesh_from_state(state.data),
        "devices": devices,
    })

    target = Path(path)
    try:
        if target.resolve() == state.path.resolve():
            raise MeshCoreError("refusing to overwrite the live state file")
    except OSError:
        pass
    try:
        _write_private_json(target, package)
    except OSError as exc:
        raise MeshCoreError(f"cannot write {target}: {exc}") from exc
    return f"exported {describe_config(package)} to {target}"


# ---------------------------------------------------------- controller identity

CONTROLLER_ADDR_MIN = 0x0001
CONTROLLER_ADDR_MAX = 0x7FFF
# When switching source address, jump the cursor forward so an address that
# was used by a previous controller is not immediately treated as a replay.
CONTROLLER_SEQ_MARGIN = 10_000


def _controller_addr(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MeshCoreError("controller address must be an integer")
    if not CONTROLLER_ADDR_MIN <= value <= CONTROLLER_ADDR_MAX:
        raise MeshCoreError(
            f"controller address must be 0x{CONTROLLER_ADDR_MIN:04x}.."
            f"0x{CONTROLLER_ADDR_MAX:04x}"
        )
    return value


def parse_unicast(text: str) -> int:
    """Parse an address the user typed, as 0x7fff or decimal 32767."""
    text = text.strip()
    if not text:
        raise MeshCoreError("enter a unicast address such as 0x7fff")
    try:
        value = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError as exc:
        raise MeshCoreError(
            f"{text!r} is not a unicast address; use 0x7fff or 32767"
        ) from exc
    return _controller_addr(value)


def _controller_ranges(data: dict) -> list[tuple[int, int]]:
    """Ranges conservatively owned by bulbs, including secondary elements.

    The state stores the first unicast of each bulb.  Provisioning allocates
    the next bulb after all elements, so the gap to the next bulb (or to
    ``next_unicast``) is treated as occupied too.
    """
    devices = data.get("devices") or {}
    points = sorted(int(key, 16) for key in devices)
    if not points:
        return []
    next_unicast = data.get("next_unicast", 0x0002)
    if isinstance(next_unicast, bool) or not isinstance(next_unicast, int):
        next_unicast = points[-1] + 1
    last_end = max(points[-1] + 1, next_unicast) - 1
    ranges = []
    for index, start in enumerate(points):
        end = points[index + 1] - 1 if index + 1 < len(points) else last_end
        ranges.append((start, end))
    return ranges


def _validate_controller_address(data: dict, address: int) -> int:
    address = _controller_addr(address)
    for start, end in _controller_ranges(data):
        if start <= address <= end:
            raise MeshCoreError(
                f"0x{address:04x} is already used by a bulb "
                f"(0x{start:04x}..0x{end:04x}); choose a free unicast address"
            )
    return address


def set_controller_address(state: State, address: int) -> str:
    """Change the source address this computer transmits from.

    The SEQ cursor is advanced by a safety margin.  A new address has no
    replay entry in the lamps; a previously used address may, so the jump
    avoids starting just below what that entry remembers.
    """
    address = _validate_controller_address(state.data, address)
    current = int(state.data.get("provisioner_addr", 0x0001))
    if address == current:
        return f"controller address is already 0x{address:04x}"
    state.data["provisioner_addr"] = address
    state.data["seq"] = min(
        0xFFFFFF, int(state.data.get("seq", 0)) + CONTROLLER_SEQ_MARGIN
    )
    state.save()
    return f"this computer now uses controller address 0x{address:04x}"


def _commit_state(state: State, data: dict) -> None:
    """Install ``data`` and save it, rolling back the in-memory copy on error."""
    previous = state.data
    state.data = data
    try:
        state.save()
    except Exception:
        state.data = previous
        raise


def apply_config(
    state: State,
    package: dict,
    *,
    replace: bool = False,
    controller_address: int | None = None,
) -> str:
    """Merge or replace the state with a validated configuration package.

    ``controller_address`` optionally gives this computer its own mesh source
    address after the import.  When the address changes, the SEQ cursor is
    advanced by a safety margin.
    """
    package = _normalise_config(package)
    current = state.data
    same = _same_mesh(current, package["mesh"])
    has_bulbs = bool(current.get("devices"))

    if has_bulbs and not same and not replace:
        raise MeshCoreError(
            "the file belongs to a different mesh; importing it would replace "
            "this computer's mesh keys and bulbs (use --replace to allow that)"
        )

    imported = copy.deepcopy(package["devices"])

    if not has_bulbs:
        new_data = {**copy.deepcopy(package["mesh"]), "devices": imported}
        summary = f"imported {describe_config(package)}"
    elif replace:
        new_data = {**copy.deepcopy(package["mesh"]), "devices": imported}
        if same:
            # Keep the higher cursor so a merge/replace can never reuse a SEQ.
            new_data["seq"] = max(int(current.get("seq", 0)), int(new_data["seq"]))
            new_data["next_unicast"] = max(
                int(current.get("next_unicast", 0x0002)),
                int(new_data["next_unicast"]),
            )
        summary = f"replaced this mesh with {describe_config(package)}"
    else:
        # Same mesh: merge the package into the existing collection.  Address
        # and UUID conflicts are detected on a copy so a failed import changes
        # nothing.
        devices = copy.deepcopy(current["devices"])
        by_uuid = {
            entry.get("uuid"): key
            for key, entry in devices.items()
            if entry.get("uuid")
        }
        added = updated = 0
        for key, entry in imported.items():
            uuid = entry.get("uuid")
            if key in devices:
                existing = devices[key]
                if existing.get("uuid") and uuid and existing["uuid"] != uuid:
                    old_key = by_uuid.get(uuid)
                    if old_key and old_key != key:
                        devices.pop(old_key, None)
                        by_uuid.pop(uuid, None)
                        devices[key] = copy.deepcopy(entry)
                        by_uuid[uuid] = key
                        updated += 1
                        continue
                    raise MeshCoreError(
                        f"cannot import {entry.get('name')!r} at {key}: that "
                        f"address is used by {existing.get('name')!r} with a "
                        "different UUID"
                    )
                devices[key] = copy.deepcopy(entry)
                if uuid:
                    by_uuid[uuid] = key
                updated += 1
                continue

            old_key = by_uuid.get(uuid) if uuid else None
            if old_key:
                devices.pop(old_key, None)
                by_uuid.pop(uuid, None)
            devices[key] = copy.deepcopy(entry)
            if uuid:
                by_uuid[uuid] = key
            added += 1

        new_data = {
            **current,
            "devices": devices,
            "seq": max(int(current.get("seq", 0)), int(package["mesh"]["seq"])),
            "next_unicast": max(
                int(current.get("next_unicast", 0x0002)),
                int(package["mesh"]["next_unicast"]),
            ),
        }
        summary = (
            f"merged {describe_config(package)}: "
            f"{added} added, {updated} updated"
        )

    if controller_address is not None:
        address = _validate_controller_address(new_data, controller_address)
        if address != int(new_data.get("provisioner_addr", 0x0001)):
            new_data["seq"] = min(
                0xFFFFFF,
                int(new_data.get("seq", 0)) + CONTROLLER_SEQ_MARGIN,
            )
        new_data["provisioner_addr"] = address
        summary += f"; controller address 0x{address:04x}"

    _commit_state(state, new_data)
    return summary


# ---------------------------------------------------------------- discovery


async def scan(state: State, timeout: float = 10.0) -> dict:
    """Listen for unprovisioned beacons and mesh proxy adverts."""
    from bleak import BleakScanner

    scanner = BleakScanner()
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
        found = scanner.discovered_devices_and_advertisement_data
        unprov = unprovisioned_from_discoveries(found)
        proxies = proxy_candidates_from_discoveries(found)
        names = {addr: dev.name for addr, (dev, _adv) in found.items()}
    finally:
        await scanner.stop()

    return {
        "unprovisioned": [
            {"address": d.device.address, "uuid": d.uuid.hex(), "oob_info": d.oob_info,
             "name": names.get(d.device.address) or ""}
            for d in unprov
        ],
        "proxies": [
            {"address": c.device.address,
             "network_id": c.parameter.hex(),
             "ours": c.identification_type == 0 and c.parameter == state.network_id,
             "identification": "network-id" if c.identification_type == 0 else "node-identity",
             "name": names.get(c.device.address) or ""}
            for c in proxies
        ],
    }


# -------------------------------------------------------------- provisioning


async def configure_node(state: State, node: MeshNode, unicast: int,
                         log_fn=log.info, *, link=None,
                         bind_attempts: int = 2) -> tuple[list[int], object]:
    """Install AppKey, read composition and bind every controllable model.

    Binds are retried on a fresh GATT session: right after provisioning the
    lamp often drops the proxy link part-way through, and a reconnect lets the
    remaining models bind instead of being skipped.  Only element-0 models are
    bound, because that is where the models used by the app live.  Config
    messages are always addressed to the node's primary unicast; the element
    address is a field inside the payload, not the destination.
    """
    resp = await node.request(
        unicast,
        config_appkey_add(state.data["netkey_index"], state.data["appkey_index"], state.appkey),
        OP_CONFIG_APPKEY_STATUS,
        dev_key=True,
        timeout=10,
    )
    st = parse_config_appkey_status(_full(resp))
    if st.status not in (0x00, 0x06):  # 0x06 = same key already stored
        raise MeshCoreError(f"AppKey Add rejected: status 0x{st.status:02x}")
    log_fn("AppKey installed")

    resp = await node.request(
        unicast,
        config_composition_data_get(0),
        OP_CONFIG_COMPOSITION_DATA_STATUS,
        dev_key=True,
        timeout=15,
    )
    comp = parse_composition_data_status(_full(resp))
    log_fn(f"composition: {comp.describe()}")

    # The node has ONE device key; config messages are addressed per element.
    entry = state.device(unicast)
    key = bytes.fromhex(entry["device_key"])

    def add_elements(target) -> None:
        for index in range(len(comp.elements)):
            target.add_device(unicast + index, key)

    add_elements(node)

    failed: list[tuple[int, str]] = []
    bound: list[int] = []
    for index, element in enumerate(comp.elements):
        if index > 0:
            # The app talks to the primary element only.  Secondary elements
            # (0x1306/0x130a/0x130b) do answer binding when config messages are
            # addressed correctly, but none of the app's commands need them.
            log_fn(
                f"element {index} models are not used by this app "
                "(secondary elements skipped)"
            )
            continue
        for model_id in element.sig_models:
            if model_id not in CONTROLLABLE_MODELS:
                continue
            last: Exception | None = None
            ok = False
            for attempt in range(1, bind_attempts + 1):
                try:
                    r = await node.request(
                        unicast,
                        config_model_app_bind(
                            unicast + index, state.data["appkey_index"], model_id
                        ),
                        OP_CONFIG_MODEL_APP_STATUS,
                        dev_key=True,
                        timeout=8,
                    )
                    status = parse_config_model_app_status(_full(r))
                    if status.status != 0x00:
                        raise MeshCoreError(f"status 0x{status.status:02x}")
                    ok = True
                    break
                except (TimeoutError, MeshCoreError) as exc:
                    last = exc
                    if attempt == bind_attempts:
                        break
                    log_fn(
                        f"0x{model_id:04x} {CONTROLLABLE_MODELS[model_id]}: "
                        f"{exc} (reconnecting)"
                    )
                    if link is None:
                        await asyncio.sleep(1.0)
                        continue
                    try:
                        await link.reconnect()
                    except Exception as reconnect_exc:  # noqa: BLE001
                        log_fn(f"reconnect failed: {reconnect_exc}")
                        continue
                    node = link.node
                    add_elements(node)
            if not ok:
                failed.append((model_id, str(last)))
                log_fn(
                    f"0x{model_id:04x} {CONTROLLABLE_MODELS[model_id]}: "
                    f"{last} (failed)"
                )
                continue
            bound.append(model_id)
            log_fn(f"0x{model_id:04x} {CONTROLLABLE_MODELS[model_id]}: bound")

    if failed:
        names = ", ".join(
            f"0x{model_id:04x} {CONTROLLABLE_MODELS[model_id]}"
            for model_id, _ in failed
        )
        raise MeshCoreError(f"could not bind required model(s): {names}")
    return bound, comp


async def configure_with_retries(
    state: State,
    unicast: int,
    *,
    log_fn=log.info,
    attempts: int = 3,
    initial_delay: float = 0.0,
    backoff: float = 3.0,
) -> tuple[list[int], object]:
    """Run :func:`configure_node` until every required model is bound.

    A freshly provisioned lamp may still be rebooting, so the whole
    configuration (find proxy, AppKey, composition, binds) is repeated on a
    fresh link before giving up.  ``initial_delay`` covers the reboot that
    starts as soon as provisioning finishes; ``backoff`` is multiplied by the
    attempt number for later retries.
    """
    if initial_delay:
        log_fn(f"waiting {initial_delay:.0f} s for the lamp to reboot…")
        await asyncio.sleep(initial_delay)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with Link(state, unicast, log_fn=log_fn) as link:
                return await configure_node(
                    state, link.node, unicast, log_fn=log_fn, link=link
                )
        except (TimeoutError, MeshCoreError) as exc:
            last = exc
            log_fn(f"configuration attempt {attempt}/{attempts} failed: {exc}")
            if attempt < attempts:
                await asyncio.sleep(backoff * attempt)
    raise MeshCoreError(f"configuration failed after {attempts} attempts: {last}")


async def provision(state: State, uuid: str | None = None, name: str = "Bulb",
                    static_oob: str | None = None, log_fn=log.info) -> int:
    """Provision one factory-reset lamp and add it to the collection."""
    transport = p0.LocalTransport()
    args = type("Args", (), {
        "device_uuid": uuid.replace("-", "").lower() if uuid else None,
        "static_oob": static_oob,
    })()
    unicast = await p0.provision(args, state.data, state.path, transport)

    entry = state.data["devices"][f"0x{unicast:04x}"]
    entry["name"] = name
    entry["bound_models"] = []
    state.save()

    # A freshly provisioned node reboots as soon as the handshake finishes and
    # often needs 20-30 s before its config server answers.  Wait for the
    # reboot, then retry the whole pass on a fresh link for up to ~1 minute.
    try:
        bound, _comp = await configure_with_retries(
            state,
            unicast,
            log_fn=log_fn,
            attempts=5,
            initial_delay=10.0,
            backoff=5.0,
        )
    except MeshCoreError as exc:
        raise MeshCoreError(
            f"bulb provisioned at 0x{unicast:04x}, but configuration failed: "
            f"{exc}. It is already on the mesh; retry with: "
            f"bulbctl configure --device 0x{unicast:04x}"
        ) from exc

    entry["bound_models"] = bound
    state.save()
    log_fn(f"added {name!r} at 0x{unicast:04x}, {len(bound)} models bound")
    return unicast


# ---------------------------------------------------------------------- link


class Link:
    """A persistent GATT-proxy connection to one lamp."""

    def __init__(self, state: State, unicast: int, log_fn=log.info) -> None:
        self.state = state
        self.unicast = unicast
        self.log = log_fn
        self.client = None
        self.bearer = None
        self.pump = None
        self.node = None
        self._range: tuple[int, int] | None = None
        self._temp_range: tuple[int, int] | None = None
        self._level: int | None = None

    def set_target(self, unicast: int) -> None:
        """Point an open session at another bulb without reconnecting.

        A GATT proxy connection relays to every node in the mesh, and some
        adapters service only one connection at a time: a second connection to
        another proxy can make both sessions go silent.  The GUI therefore
        keeps one session and switches its target instead of opening a link
        per bulb.
        """
        if unicast == self.unicast:
            return
        entry = self.state.device(unicast)
        self.unicast = unicast
        # Per-node caches must not leak between bulbs.
        self._range = None
        self._temp_range = None
        self._level = None
        if self.node is not None:
            self.node.add_device(unicast, bytes.fromhex(entry["device_key"]))
            self.log(f"targeting 0x{unicast:04x}")

    async def __aenter__(self) -> "Link":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self.node is not None

    async def connect(self) -> None:
        if self.connected:
            return
        entry = self.state.device(self.unicast)
        transport = p0.LocalTransport()

        last: Exception | None = None
        for attempt in range(1, 4):
            try:
                target = await p0._find_proxy_target(
                    self.state.data, transport, self.unicast
                )
                self.log(f"proxy {target.address}")
                self.client, self.bearer = await p0.open_gatt_session(
                    transport, target, provisioning=False,
                    on_message=self._dispatch, attempts=2,
                )
                break
            except p0.BearerError as exc:
                last = exc
                self.log(f"connect attempt {attempt}/3 failed")
                await asyncio.sleep(2.0)
        else:
            raise MeshCoreError(f"could not connect to bulb 0x{self.unicast:04x}: {last}")

        self.pump = BearerPump(self.bearer, MSG_TYPE_NETWORK_PDU)
        self.pump.on_error = lambda exc: log.error("network TX failed: %s", exc)

        node = MeshNode(
            netkey=self.state.netkey,
            appkey=self.state.appkey,
            iv_index=self.state.data["iv_index"],
            src_addr=self.state.data["provisioner_addr"],
            send_network_pdu=self._send_network,
            seq=self.state.data["seq"],
        )
        node.add_device(self.unicast, bytes.fromhex(entry["device_key"]))
        self.node = node
        self.pump.start()

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if self.node is not None and msg_type == MSG_TYPE_NETWORK_PDU:
            self.node.handle_network_pdu(payload)

    def _send_network(self, raw: bytes) -> None:
        self.pump.put(raw)
        self.state.data["seq"] = self.node.ctx.seq
        self.state.save()

    async def close(self) -> None:
        if self.pump:
            await self.pump.stop()
        if self.bearer:
            await self.bearer.stop()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.node = self.bearer = self.pump = self.client = None

    async def reconnect(self) -> None:
        """Drop the GATT session and find the proxy again.

        Mesh lamps regularly drop the proxy link right after provisioning or
        after a period of silence; a fresh connect is the only reliable way to
        keep talking to them.
        """
        await self.close()
        await self.connect()

    async def request(self, payload: bytes, opcode: int, *, timeout: float = 6.0,
                      dev_key: bool = False):
        if not self.connected:
            await self.connect()
        return await self.node.request(
            self.unicast, payload, opcode, timeout=timeout, dev_key=dev_key
        )

    # -- light control
    @property
    def models(self) -> list[int]:
        return self.state.device(self.unicast).get("bound_models") or []

    def has(self, model_id: int) -> bool:
        return model_id in self.models

    async def lightness_range(self) -> tuple[int, int]:
        if self._range:
            return self._range
        try:
            r = await self.request(encode_opcode(OP_LIGHT_LIGHTNESS_RANGE_GET),
                                   OP_LIGHT_LIGHTNESS_RANGE_STATUS)
            p = r.params
            if len(p) >= 5 and p[0] == 0:
                mn = int.from_bytes(p[1:3], "little") or 1
                mx = int.from_bytes(p[3:5], "little") or 0xFFFF
                if mx >= mn:
                    self._range = (mn, mx)
                    return self._range
        except TimeoutError:
            pass
        self._range = (1, 0xFFFF)
        return self._range

    async def ensure_lightness_range(self) -> tuple[int, int]:
        """Widen the lamp's Light Lightness range to the full 16-bit scale.

        These lamps ship with a small range (1..50 on older firmware, 1..100
        on newer firmware).  The newer firmware also renders the HSL lightness
        field as an absolute 16-bit value, so an HSL Set of 100 on the default
        range is shown at roughly 0.15% brightness.  A one-time Range Set to
        1..65535 makes both behaviours agree; brightness percentages are then
        scaled against 65535.
        """
        mn, mx = await self.lightness_range()
        entry = self.state.device(self.unicast)

        if MODEL_LIGHT_LIGHTNESS_SETUP_SERVER not in self.models:
            try:
                r = await self.node.request(
                    self.unicast,
                    config_model_app_bind(
                        self.unicast,
                        self.state.data["appkey_index"],
                        MODEL_LIGHT_LIGHTNESS_SETUP_SERVER,
                    ),
                    OP_CONFIG_MODEL_APP_STATUS,
                    dev_key=True,
                    timeout=8,
                )
                st = parse_config_model_app_status(_full(r))
                if st.status == 0x00:
                    models = entry.setdefault("bound_models", [])
                    if MODEL_LIGHT_LIGHTNESS_SETUP_SERVER not in models:
                        models.append(MODEL_LIGHT_LIGHTNESS_SETUP_SERVER)
                        self.state.save()
            except (TimeoutError, MeshCoreError) as exc:
                self.log(f"could not bind Light Lightness Setup Server: {exc}")

        # Work out which scale the stored level was written on.  Legacy state
        # files have no level_range_max: a still-small reported range is that
        # scale (Bulb 1 uses 1..50), otherwise assume the old 1..100 default.
        stored_max = int(entry.get("level_range_max") or 0)
        stored_level = int(entry.get("last_level") or 0)
        if stored_max:
            old_max = stored_max
        elif 0 < stored_level <= 100:
            old_max = mx if mx <= 100 else 100
        else:
            old_max = mx

        if not (mx >= 0xFFFF and mn <= 1):
            try:
                r = await self.request(
                    encode_opcode(OP_LIGHT_LIGHTNESS_RANGE_SET)
                    + (1).to_bytes(2, "little")
                    + (0xFFFF).to_bytes(2, "little"),
                    OP_LIGHT_LIGHTNESS_RANGE_STATUS,
                    timeout=8,
                )
                p = r.params
                if len(p) >= 5 and p[0] == 0:
                    new_mn = int.from_bytes(p[1:3], "little") or 1
                    new_mx = int.from_bytes(p[3:5], "little") or 0xFFFF
                    self._range = (new_mn, new_mx)
                    if new_mx > mx:
                        self.log(f"lightness range widened to {new_mn}..{new_mx}")
                    else:
                        self.log(
                            f"lightness range unchanged at {new_mn}..{new_mx}; "
                            "colour may render dim"
                        )
                    mn, mx = new_mn, new_mx
            except (TimeoutError, MeshCoreError) as exc:
                self.log(f"could not widen lightness range: {exc}")

        changed = False
        if old_max and mx and old_max != mx:
            level = int(entry.get("last_level") or 0)
            if 0 < level <= old_max:
                entry["last_level"] = max(
                    mn, min(mx, round(level / old_max * mx))
                )
                changed = True
            if self._level and 0 < self._level <= old_max:
                self._level = max(
                    mn, min(mx, round(self._level / old_max * mx))
                )
                changed = True
        if entry.get("level_range_max") != mx:
            entry["level_range_max"] = mx
            changed = True
        if changed:
            self.state.save()
        return mn, mx

    async def temperature_range(self) -> tuple[int, int]:
        """The lamp's actual CTL temperature range in Kelvin."""
        if self._temp_range:
            return self._temp_range
        try:
            r = await self.request(
                encode_opcode(OP_LIGHT_CTL_TEMPERATURE_RANGE_GET),
                OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS,
            )
            p = r.params
            if len(p) >= 5:
                mn = int.from_bytes(p[1:3], "little")
                mx = int.from_bytes(p[3:5], "little")
                if 0 < mn < mx:
                    self._temp_range = (mn, mx)
                    return self._temp_range
        except TimeoutError:
            pass
        self._temp_range = (2000, 6500)
        return self._temp_range

    async def status(self) -> dict:
        out: dict = {"unicast": self.unicast}
        mn, mx = await self.ensure_lightness_range()
        out["range"] = (mn, mx)
        if self.has(MODEL_LIGHT_LIGHTNESS_SERVER):
            r = await self.request(light_lightness_get(), OP_LIGHT_LIGHTNESS_STATUS)
            st = parse_light_lightness_status(_full(r))
            out["level"] = st.present_lightness
            out["level_pct"] = round(st.present_lightness * 100 / mx) if mx else 0
            out["on"] = st.present_lightness > 0
            self._level = st.present_lightness
        elif self.has(MODEL_GENERIC_ONOFF_SERVER):
            r = await self.request(generic_onoff_get(), OP_GENERIC_ONOFF_STATUS)
            out["on"] = bool(parse_generic_onoff_status(_full(r)).present_onoff)
        if self.has(MODEL_LIGHT_CTL_SERVER):
            try:
                r = await self.request(light_ctl_get(), OP_LIGHT_CTL_STATUS)
                st = parse_light_ctl_status(_full(r))
                out["temperature"] = st.present_temperature
                out["ctl_lightness"] = st.present_lightness
                out["temperature_range"] = await self.temperature_range()
            except TimeoutError:
                out["temperature"] = None
        entry = self.state.device(self.unicast)
        if out.get("level"):
            remembered = int(entry.get("last_level") or 0)
            if remembered != out["level"] or entry.get("level_range_max") != mx:
                entry["last_level"] = out["level"]
                entry["level_range_max"] = mx
                self.state.save()
        if self.has(MODEL_LIGHT_HSL_SERVER):
            try:
                r = await self.request(
                    encode_opcode(OP_LIGHT_HSL_GET), OP_LIGHT_HSL_STATUS
                )
                p = r.params
                if len(p) >= 6:
                    out["hue"] = int.from_bytes(p[2:4], "little")
                    out["saturation"] = int.from_bytes(p[4:6], "little")
                    out["hsl_lightness"] = int.from_bytes(p[0:2], "little")
                    if entry.get("mode") == "colour":
                        entry["hue"] = out["hue"]
                        entry["saturation"] = out["saturation"]
                        self.state.save()
            except TimeoutError:
                pass
        out["mode"] = entry.get("mode")
        out.setdefault("hue", entry.get("hue"))
        out.setdefault("saturation", entry.get("saturation"))
        return out

    async def set_power(self, on: bool) -> None:
        if self.has(MODEL_LIGHT_LIGHTNESS_SERVER):
            mn, mx = await self.ensure_lightness_range()
            if on:
                # Restore the last non-zero brightness instead of blasting 100%.
                entry = self.state.device(self.unicast)
                remembered = int(entry.get("last_level") or 0)
                value = max(mn, min(mx, self._level or remembered or mx))
            else:
                value = 0
            await self.request(light_lightness_set(value, _tid()), OP_LIGHT_LIGHTNESS_STATUS)
            self._level = value
        else:
            await self.request(generic_onoff_set(on, _tid()), OP_GENERIC_ONOFF_STATUS)

    async def set_level(self, pct: int) -> None:
        mn, mx = await self.ensure_lightness_range()
        value = 0 if pct <= 0 else max(mn, min(mx, round(pct / 100 * mx)))
        await self.request(light_lightness_set(value, _tid()), OP_LIGHT_LIGHTNESS_STATUS)
        self._level = value
        if value:
            entry = self.state.device(self.unicast)
            if (
                entry.get("last_level") != value
                or entry.get("level_range_max") != mx
            ):
                entry["last_level"] = value
                entry["level_range_max"] = mx
                self.state.save()

    async def set_temperature(self, kelvin: int) -> None:
        mn, mx = await self.ensure_lightness_range()
        entry = self.state.device(self.unicast)
        level = max(mn, min(mx, self._level or int(entry.get("last_level") or 0) or mx))
        await self.request(
            light_ctl_set(level, kelvin, 0, _tid()), OP_LIGHT_CTL_STATUS
        )
        entry["mode"] = "temperature"
        entry["temperature"] = kelvin
        entry["last_level"] = level
        entry["level_range_max"] = mx
        self._level = level
        self.state.save()

    async def set_colour(self, rgb_hex: str, lightness: int | None = None) -> tuple[int, int]:
        """Set hue/saturation for #RRGGBB, keeping the current brightness."""
        hue, sat = hue_sat_from_hex(rgb_hex)
        return await self.set_colour_hs(hue, sat, lightness)

    async def set_colour_hs(self, hue: int, saturation: int,
                            lightness: int | None = None) -> tuple[int, int]:
        """Hue 0..360, saturation 0..100, and a 16-bit Light Lightness level.

        The lamp's HSL lightness field is measured on the same 1..65535 scale
        as Light Lightness (the scale is widened by
        :meth:`ensure_lightness_range`).
        """
        mn, mx = await self.ensure_lightness_range()
        hue = max(0, min(360, round(hue)))
        saturation = max(0, min(100, round(saturation)))
        entry = self.state.device(self.unicast)
        if lightness is None:
            level = self._level or int(entry.get("last_level") or 0) or mx
        else:
            level = lightness
        level = 0 if level < mn else max(mn, min(mx, round(level)))
        r = await self.request(
            light_hsl_set(level, hue, saturation, _tid()), OP_LIGHT_HSL_STATUS
        )
        self._level = level
        if level:
            entry["last_level"] = level
        entry["level_range_max"] = mx
        entry["mode"] = "colour"
        entry["hue"] = hue
        entry["saturation"] = saturation
        self.state.save()
        # Status echoes (lightness, hue, saturation) in the lamp's own units.
        if len(r.params) >= 6:
            return (
                int.from_bytes(r.params[2:4], "little"),
                int.from_bytes(r.params[4:6], "little"),
            )
        return hue, saturation

    async def composition(self):
        r = await self.request(
            config_composition_data_get(0), OP_CONFIG_COMPOSITION_DATA_STATUS,
            timeout=15, dev_key=True,
        )
        return parse_composition_data_status(_full(r))


# ------------------------------------------------------------------- helpers


def _full(msg) -> bytes:
    return encode_opcode(msg.opcode) + msg.params


def _tid() -> int:
    return secrets.randbelow(256)


def hue_sat_from_hex(text: str) -> tuple[int, int]:
    """#RRGGBB -> (hue 0..360, saturation 0..100).

    Standard HSL hue/saturation. The lamp's HSL *lightness* field is its
    brightness (1..50), not colour lightness, so it is supplied separately.
    """
    h = text.strip().lstrip("#")
    if len(h) != 6:
        raise MeshCoreError(f"expected RRGGBB, got {text!r}")
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError as exc:
        raise MeshCoreError(f"invalid colour {text!r}") from exc
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    lightness = (mx + mn) / 2
    if d == 0:
        return 0, 0
    if mx == r:
        hue = ((g - b) / d) % 6
    elif mx == g:
        hue = (b - r) / d + 2
    else:
        hue = (r - g) / d + 4
    sat = d / (1 - abs(2 * lightness - 1)) if lightness not in (0, 1) else 0
    return round(hue * 60) % 360, round(sat * 100)


def light_hsl_set(lightness: int, hue: int, saturation: int, tid: int) -> bytes:
    return (
        encode_opcode(OP_LIGHT_HSL_SET)
        + lightness.to_bytes(2, "little")
        + hue.to_bytes(2, "little")
        + saturation.to_bytes(2, "little")
        + bytes([tid])
    )
