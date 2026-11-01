"""Offline regression test for HSL colour and the Light Lightness scale.

These lamps ship with a 1..100 Light Lightness range.  Their HSL model treats
the lightness field as a 16-bit value, so HSL Set with 100 on the default
range renders at ~0.15% and the lamp stays dim.  The app must widen the range
to 1..65535 once, migrate stored levels, and then send 16-bit lightness.
"""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import meshcore

UNICAST = 0x001E


def make_state(root: Path, bound_models: list[int]) -> meshcore.State:
    state = meshcore.State(root / "state.json")
    state.add_device(
        UNICAST,
        name="Bulb 2",
        uuid="11" * 16,
        address="F0:D1:B8:1E:31:C6",
        device_key=b"\x22" * 16,
        bound_models=bound_models,
    )
    entry = state.device(UNICAST)
    entry.update(
        {
            "last_level": 100,
            "mode": "temperature",
            "hue": 120,
            "saturation": 100,
        }
    )
    state.save()
    return state


class FakeNode:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bytes]] = []

    async def request(self, addr, payload, opcode, *, dev_key=False, timeout=6.0):
        self.calls.append((addr, payload))
        return SimpleNamespace(
            opcode=meshcore.OP_CONFIG_MODEL_APP_STATUS,
            params=bytes([0]) + UNICAST.to_bytes(2, "little") + b"\x00\x00"
            + (0x1301).to_bytes(2, "little"),
        )


def install_transport(link: meshcore.Link, sim: dict, node: FakeNode | None = None):
    async def fake_request(payload: bytes, opcode: int, *, timeout=6.0,
                            dev_key=False):
        sim["calls"].append((opcode, payload))
        head = payload[:2]

        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_GET):
            mn, mx = sim["range"]
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_RANGE_STATUS,
                params=bytes([0]) + mn.to_bytes(2, "little")
                + mx.to_bytes(2, "little"),
            )

        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET):
            mn = int.from_bytes(payload[2:4], "little")
            mx = int.from_bytes(payload[4:6], "little")
            sim["range"] = (mn, mx)
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_RANGE_STATUS,
                params=bytes([0]) + mn.to_bytes(2, "little")
                + mx.to_bytes(2, "little"),
            )

        if head == meshcore.encode_opcode(0x824C):  # Light Lightness Set
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_STATUS,
                params=payload[2:4],
            )

        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_HSL_SET):
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_HSL_STATUS,
                params=payload[2:8],
            )

        raise AssertionError(f"unexpected request opcode 0x{opcode:04x}")

    link.request = fake_request  # type: ignore[method-assign]
    if node is not None:
        link.node = node  # type: ignore[assignment]


root = Path(tempfile.mkdtemp(prefix="bulbctl-colour-test-"))

# --- 1. Range widening, level migration, and HSL units --------------------
state = make_state(root, [0x1000, 0x1300, 0x1307, 0x1301])
link = meshcore.Link(state, UNICAST)
sim = {"range": (1, 100), "calls": []}
install_transport(link, sim)

mn, mx = asyncio.run(link.ensure_lightness_range())
assert (mn, mx) == (1, 0xFFFF), (mn, mx)
assert sim["range"] == (1, 0xFFFF), sim["range"]
entry = state.device(UNICAST)
assert entry["last_level"] == 0xFFFF, entry["last_level"]

range_sets = [
    payload for opcode, payload in sim["calls"]
    if payload[:2] == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET)
]
assert len(range_sets) == 1, range_sets
assert range_sets[0][2:6] == b"\x01\x00\xff\xff", range_sets[0].hex()

# A second call must be a no-op (the widened range is cached).
before = len(sim["calls"])
assert asyncio.run(link.ensure_lightness_range()) == (1, 0xFFFF)
assert len(sim["calls"]) == before, "already-wide range must not be rewritten"

# Percent brightness is scaled to the 16-bit range.
asyncio.run(link.set_level(50))
set_payload = [
    payload for opcode, payload in sim["calls"]
    if payload[:2] == meshcore.encode_opcode(0x824C)
][-1]
level = int.from_bytes(set_payload[2:4], "little")
assert level == 0x8000, hex(level)
assert state.device(UNICAST)["last_level"] == 0x8000

# HSL Set keeps hue in degrees and saturation in percent, but sends the
# current 16-bit brightness.
hue, sat = asyncio.run(link.set_colour_hs(0, 100))
assert (hue, sat) == (0, 100), (hue, sat)
hsl_payload = [
    payload for opcode, payload in sim["calls"]
    if payload[:2] == meshcore.encode_opcode(meshcore.OP_LIGHT_HSL_SET)
][-1]
assert hsl_payload[2:4] == b"\x00\x80", hsl_payload.hex()  # 0x8000 brightness
assert hsl_payload[4:6] == b"\x00\x00", hsl_payload.hex()  # hue 0
assert hsl_payload[6:8] == b"\x64\x00", hsl_payload.hex()  # saturation 100
assert state.device(UNICAST)["mode"] == "colour"

# An explicit 16-bit lightness is passed through untouched.
asyncio.run(link.set_colour_hs(120, 100, lightness=0xFFFF))
hsl_payload = [
    payload for opcode, payload in sim["calls"]
    if payload[:2] == meshcore.encode_opcode(meshcore.OP_LIGHT_HSL_SET)
][-1]
assert hsl_payload[2:4] == b"\xff\xff", hsl_payload.hex()
assert hsl_payload[4:6] == b"\x78\x00", hsl_payload.hex()  # hue 120
assert hsl_payload[6:8] == b"\x64\x00", hsl_payload.hex()  # saturation 100

state.release()

# --- 2. Missing Setup Server is bound at the primary address --------------
state2 = make_state(root / "second", [0x1000, 0x1300, 0x1307])
link2 = meshcore.Link(state2, UNICAST)
node = FakeNode()
sim2 = {"range": (1, 100), "calls": []}
install_transport(link2, sim2, node=node)

mn, mx = asyncio.run(link2.ensure_lightness_range())
assert (mn, mx) == (1, 0xFFFF), (mn, mx)
assert len(node.calls) == 1, node.calls
addr, payload = node.calls[0]
assert addr == UNICAST, hex(addr)
assert payload[:2] == meshcore.encode_opcode(0x803D)  # Config Model App Bind
assert payload[2:4] == UNICAST.to_bytes(2, "little"), payload.hex()
assert 0x1301 in state2.device(UNICAST)["bound_models"]

state2.release()

# --- 3. Old levels are migrated even if the lamp is already wide ----------
state3 = make_state(root / "third", [0x1000, 0x1300, 0x1307, 0x1301])
link3 = meshcore.Link(state3, UNICAST)
sim3 = {"range": (1, 0xFFFF), "calls": []}
install_transport(link3, sim3)

mn, mx = asyncio.run(link3.ensure_lightness_range())
assert (mn, mx) == (1, 0xFFFF), (mn, mx)
assert state3.device(UNICAST)["last_level"] == 0xFFFF, state3.device(UNICAST)
assert not any(
    payload[:2] == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET)
    for _opcode, payload in sim3["calls"]
), "an already-wide lamp must not get another Range Set"

state3.release()

# --- 4. A 1..50 bulb at its maximum keeps full brightness -----------------
# Bulb 1 ships with a 1..50 range, so a stored level of 50 means 100%, not
# 50%.  Migration must use the old range maximum, not a hard-coded 100.
state4 = make_state(root / "fourth", [0x1000, 0x1300, 0x1307, 0x1301])
state4.device(UNICAST)["last_level"] = 50
state4.save()
link4 = meshcore.Link(state4, UNICAST)
sim4 = {"range": (1, 50), "calls": []}
install_transport(link4, sim4)

mn, mx = asyncio.run(link4.ensure_lightness_range())
assert (mn, mx) == (1, 0xFFFF), (mn, mx)
assert state4.device(UNICAST)["last_level"] == 0xFFFF, state4.device(UNICAST)
assert state4.device(UNICAST)["level_range_max"] == 0xFFFF

state4.release()

print("colour-scale regression test passed")
