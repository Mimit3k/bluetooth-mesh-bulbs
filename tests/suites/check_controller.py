"""Offline tests for per-computer controller addresses."""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import meshcore


def add(state, unicast, name, uuid, key_byte):
    state.add_device(
        unicast,
        name=name,
        uuid=uuid,
        address=f"AA:BB:CC:DD:EE:{unicast:02X}",
        device_key=bytes([key_byte]) * 16,
        bound_models=[0x1000, 0x1300, 0x1303, 0x1307],
    )


def assert_raises(fn, contains):
    try:
        fn()
    except meshcore.MeshCoreError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError(f"expected MeshCoreError containing {contains!r}")


root = Path(tempfile.mkdtemp(prefix="bulbctl-controller-test-"))

# A two-bulb mesh: 0x0002..0x0005 and 0x0006..0x0009 are bulb address space.
state = meshcore.State(root / "state.json")
add(state, 0x0002, "Desk", "11" * 16, 0x11)
add(state, 0x0006, "Lamp", "22" * 16, 0x22)
state.data["next_unicast"] = 0x000A
state.data["seq"] = 1234
state.save()

assert meshcore.parse_unicast("0x7fff") == 0x7FFF
assert meshcore.parse_unicast("32767") == 0x7FFF
assert meshcore.parse_unicast(" 0x0001 ") == 0x0001
for bad in ("", "nope", "0x8000", "0x0000", "-1", "0x10000"):
    assert_raises(lambda text=bad: meshcore.parse_unicast(text), "")

margin = meshcore.CONTROLLER_SEQ_MARGIN
seq0 = 1234

devices_before = copy.deepcopy(state.devices)
summary = meshcore.set_controller_address(state, 0x7FFF)
assert "0x7fff" in summary
assert state.data["provisioner_addr"] == 0x7FFF
assert state.data["seq"] == seq0 + margin, "changing the address must advance SEQ"
assert state.devices == devices_before
saved = json.loads(state.path.read_text())
assert saved["provisioner_addr"] == 0x7FFF
assert saved["seq"] == seq0 + margin

# Changing again is a no-op and keeps the saved value.
assert "already 0x7fff" in meshcore.set_controller_address(state, 0x7FFF)

# Addresses occupied by a bulb, or outside the unicast range, are refused.
for bad in (0x0002, 0x0003, 0x0005, 0x0006, 0x0009, 0x0000, 0x8000):
    before = copy.deepcopy(state.data)
    assert_raises(
        lambda value=bad: meshcore.set_controller_address(state, value),
        "" if bad not in (0x0000, 0x8000) else "controller address",
    )
    assert state.data == before, f"failed change to 0x{bad:04x} altered state"

# Free gaps below the high-water mark, and high addresses, are accepted.
for good in (0x0001, 0x000A, 0x0100, 0x7FFE):
    meshcore.set_controller_address(state, good)
    assert state.data["provisioner_addr"] == good
meshcore.set_controller_address(state, 0x7FFF)
package_seq = state.data["seq"]
assert package_seq == seq0 + 6 * margin, package_seq

# An export carries this computer's address.
package_file = root / "export.json"
meshcore.export_config(state, package_file, kind="mesh")
package = meshcore.read_config(package_file)
assert package["mesh"]["provisioner_addr"] == 0x7FFF
assert package["mesh"]["seq"] == package_seq

# A fresh computer can adopt an address during import, atomically.
fresh = meshcore.State(root / "fresh.json")
result = meshcore.apply_config(fresh, package, controller_address=0x7FFF)
assert "controller address 0x7fff" in result
assert fresh.data["provisioner_addr"] == 0x7FFF
assert fresh.data["seq"] == package_seq
assert set(fresh.devices) == {"0x0002", "0x0006"}

# A merge keeps the local controller address by default...
local = meshcore.State(root / "local.json")
local.data = copy.deepcopy(state.data)
local.data["provisioner_addr"] = 0x7FFE
local.data["seq"] = 2000
local.save()
meshcore.apply_config(local, package)
assert local.data["provisioner_addr"] == 0x7FFE, "merge must keep local identity"
assert local.data["seq"] == package_seq

# ...and can change it explicitly.
meshcore.apply_config(local, package, controller_address=0x7FFD)
assert local.data["provisioner_addr"] == 0x7FFD
assert local.data["seq"] == package_seq + margin

# Replace follows the file's address unless an explicit one is supplied.
replaced = meshcore.State(root / "replaced.json")
replaced.data = copy.deepcopy(state.data)
replaced.data["provisioner_addr"] = 0x7FFE
replaced.save()
meshcore.apply_config(replaced, package, replace=True)
assert replaced.data["provisioner_addr"] == 0x7FFF
meshcore.apply_config(replaced, package, replace=True, controller_address=0x7FFC)
assert replaced.data["provisioner_addr"] == 0x7FFC
assert replaced.data["seq"] == package_seq + margin

# A bad explicit address aborts before the import is committed.
before = copy.deepcopy(fresh.data)
assert_raises(
    lambda: meshcore.apply_config(fresh, package, controller_address=0x0002),
    "already used by a bulb",
)
assert fresh.data == before

# CLI: show, change, and import with an address.
cli = str(ROOT / "bulbctl.py")
cli_state = root / "cli.json"
shutil.copy(state.path, cli_state)
state.release()

out = subprocess.run(
    [sys.executable, cli, "--state", str(cli_state), "controller-address"],
    check=True,
    capture_output=True,
    text=True,
)
assert "controller address: 0x7fff" in out.stdout, out.stdout

out = subprocess.run(
    [
        sys.executable,
        cli,
        "--state",
        str(cli_state),
        "controller-address",
        "0x7ff0",
    ],
    check=True,
    capture_output=True,
    text=True,
)
assert "0x7ff0" in out.stdout, out.stdout
assert json.loads(cli_state.read_text())["provisioner_addr"] == 0x7FF0

out = subprocess.run(
    [
        sys.executable,
        cli,
        "--state",
        str(cli_state),
        "controller-address",
        "0x0003",
    ],
    capture_output=True,
    text=True,
)
assert out.returncode == 2 and "already used by a bulb" in out.stdout + out.stderr
assert json.loads(cli_state.read_text())["provisioner_addr"] == 0x7FF0

out = subprocess.run(
    [
        sys.executable,
        cli,
        "--state",
        str(root / "cli-import.json"),
        "import",
        str(package_file),
        "--controller-address",
        "0x7ff1",
        "--yes",
    ],
    check=True,
    capture_output=True,
    text=True,
)
assert "controller address 0x7ff1" in out.stdout, out.stdout
imported = json.loads((root / "cli-import.json").read_text())
assert imported["provisioner_addr"] == 0x7FF1
assert imported["seq"] == package_seq + margin

print("controller-address tests passed")
