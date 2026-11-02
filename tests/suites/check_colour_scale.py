"""Offline regression test for the two lightness scales.

These lamps scale Light Lightness, CTL lightness and Light HSL lightness to
their factory range (1..50 on older firmware, 1..100 on newer firmware).  An
earlier build widened the range to 1..65535 so HSL could be sent as a
percentage; the sliders then moved but the LEDs stayed at full brightness.
The HSL lightness also behaves like HSL's *L*: near the range maximum the
colour washes out to white, while half the range is the brightest saturated
colour.  This suite locks in the corrected model:

* never widen the factory range;
* scale white brightness (Light Lightness / CTL) to the factory range;
* send colour brightness (HSL) on the same range, in its saturated half;
* remember brightness as a percentage so it survives mode switches.
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
ALL_MODELS = [0x1000, 0x1300, 0x1301, 0x1303, 0x1307]


def make_state(root: Path, bound_models: list[int] | None = None) -> meshcore.State:
    state = meshcore.State(root / "state.json")
    state.add_device(
        UNICAST,
        name="Bulb 2",
        uuid="11" * 16,
        address="F0:D1:B8:1E:31:C6",
        device_key=b"\x22" * 16,
        bound_models=list(bound_models or ALL_MODELS),
    )
    entry = state.device(UNICAST)
    entry.update(
        {
            "last_level_pct": 100,
            "factory_range_max": 100,
            "mode": "temperature",
            "hue": 120,
            "saturation": 100,
            "temperature": 2700,
        }
    )
    state.save()
    return state


def u16(data: bytes, index: int = 0) -> int:
    return int.from_bytes(data[index:index + 2], "little")


class Sim:
    """A lamp that mimics the firmware's scale behaviour."""

    def __init__(self, lightness_range: tuple[int, int]) -> None:
        self.range = lightness_range
        self.calls: list[tuple[int, bytes]] = []
        self.onoff = 1
        self.ll = lightness_range[1]
        self.ctl_lightness = lightness_range[1]
        self.ctl_temperature = 2700
        # HSL Status clamps its lightness to the factory maximum, even though
        # the LED output follows the absolute value that was sent.
        self.hsl = (lightness_range[1], 0, 0)


def install_transport(link: meshcore.Link, sim: Sim, node=None) -> None:
    async def fake_request(payload: bytes, opcode: int, *, timeout: float = 6.0,
                           dev_key: bool = False):
        sim.calls.append((opcode, payload))
        head = payload[:2]

        if head == meshcore.encode_opcode(0x8201):
            return SimpleNamespace(
                opcode=meshcore.OP_GENERIC_ONOFF_STATUS, params=bytes([sim.onoff])
            )
        if head == meshcore.encode_opcode(0x8202):  # Generic OnOff Set
            sim.onoff = payload[2]
            return SimpleNamespace(
                opcode=meshcore.OP_GENERIC_ONOFF_STATUS, params=bytes([sim.onoff])
            )

        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_GET):
            mn, mx = sim.range
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_RANGE_STATUS,
                params=bytes([0]) + mn.to_bytes(2, "little")
                + mx.to_bytes(2, "little"),
            )
        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET):
            mn = u16(payload, 2)
            mx = u16(payload, 4)
            sim.range = (mn, mx)
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_RANGE_STATUS,
                params=bytes([0]) + mn.to_bytes(2, "little")
                + mx.to_bytes(2, "little"),
            )

        if head == meshcore.light_lightness_get():
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_STATUS,
                params=sim.ll.to_bytes(2, "little"),
            )
        if head == meshcore.encode_opcode(0x824C):  # Light Lightness Set
            sim.ll = u16(payload, 2)
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_LIGHTNESS_STATUS,
                params=payload[2:4],
            )

        if head == meshcore.light_ctl_get():
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_CTL_STATUS,
                params=sim.ctl_lightness.to_bytes(2, "little")
                + sim.ctl_temperature.to_bytes(2, "little"),
            )
        if head == meshcore.encode_opcode(0x825E):  # Light CTL Set
            sim.ctl_lightness = u16(payload, 2)
            sim.ctl_temperature = u16(payload, 4)
            sim.ll = sim.ctl_lightness  # the lamp mirrors the two models
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_CTL_STATUS,
                params=payload[2:6],
            )
        if head == meshcore.encode_opcode(
            meshcore.OP_LIGHT_CTL_TEMPERATURE_RANGE_GET
        ):
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS,
                params=bytes([0]) + (1000).to_bytes(2, "little")
                + (12000).to_bytes(2, "little"),
            )

        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_HSL_GET):
            lightness, hue, sat = sim.hsl
            clamped = min(lightness, sim.range[1])
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_HSL_STATUS,
                params=clamped.to_bytes(2, "little")
                + hue.to_bytes(2, "little") + sat.to_bytes(2, "little"),
            )
        if head == meshcore.encode_opcode(meshcore.OP_LIGHT_HSL_SET):
            lightness, hue, sat = (
                u16(payload, 2), u16(payload, 4), u16(payload, 6)
            )
            sim.hsl = (min(lightness, sim.range[1]), hue, sat)
            return SimpleNamespace(
                opcode=meshcore.OP_LIGHT_HSL_STATUS,
                params=payload[2:8],
            )

        raise AssertionError(f"unexpected request opcode 0x{opcode:04x}")

    link.request = fake_request  # type: ignore[method-assign]
    if node is not None:
        link.node = node  # type: ignore[assignment]


def sent(sim: Sim, opcode: int) -> list[bytes]:
    head = meshcore.encode_opcode(opcode)
    return [payload for _opcode, payload in sim.calls if payload[:2] == head]


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


root = Path(tempfile.mkdtemp(prefix="bulbctl-colour-test-"))

# --- 1. The factory range is kept and white brightness is scaled to it -----
state = make_state(root / "factory")
link = meshcore.Link(state, UNICAST)
sim = Sim((1, 100))
install_transport(link, sim)

mn, mx = asyncio.run(link.ensure_lightness_range())
assert (mn, mx) == (1, 100), (mn, mx)
assert state.device(UNICAST)["factory_range_max"] == 100
assert not sent(sim, meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET), (
    "the factory range must never be rewritten"
)

# 50% white -> CTL lightness 50 on a 1..100 lamp (not 0x8000).
asyncio.run(link.set_level(50))
ctl_sets = sent(sim, 0x825E)
assert u16(ctl_sets[-1], 2) == 50, ctl_sets[-1].hex()
assert u16(ctl_sets[-1], 4) == 2700, ctl_sets[-1].hex()
assert state.device(UNICAST)["last_level_pct"] == 50

# Changing temperature keeps the same scaled brightness.
asyncio.run(link.set_temperature(4000))
ctl_sets = sent(sim, 0x825E)
assert u16(ctl_sets[-1], 2) == 50, ctl_sets[-1].hex()
assert u16(ctl_sets[-1], 4) == 4000, ctl_sets[-1].hex()
assert state.device(UNICAST)["last_level_pct"] == 50

# --- 2. HSL lightness uses the saturated half of the factory range ---------
hue, sat = asyncio.run(link.set_colour_hs(0, 100))
assert (hue, sat) == (0, 100), (hue, sat)
hsl_sets = sent(sim, meshcore.OP_LIGHT_HSL_SET)
assert u16(hsl_sets[-1], 2) == 25, hsl_sets[-1].hex()  # half of 1..100
assert u16(hsl_sets[-1], 4) == 0, hsl_sets[-1].hex()  # hue 0
assert u16(hsl_sets[-1], 6) == 100, hsl_sets[-1].hex()  # saturation 100
entry = state.device(UNICAST)
assert entry["mode"] == "colour"
assert entry["last_level_pct"] == 50

# An explicit percentage is scaled onto 0..(range // 2).
asyncio.run(link.set_colour_hs(120, 100, lightness=25))
hsl_sets = sent(sim, meshcore.OP_LIGHT_HSL_SET)
assert u16(hsl_sets[-1], 2) == 13, hsl_sets[-1].hex()  # 25% of 1..100
assert u16(hsl_sets[-1], 4) == 120, hsl_sets[-1].hex()

# Moving the brightness slider in colour mode sends HSL, preserving hue/sat.
before_ctl = len(sent(sim, 0x825E))
asyncio.run(link.set_level(25))
hsl_sets = sent(sim, meshcore.OP_LIGHT_HSL_SET)
assert u16(hsl_sets[-1], 2) == 13, hsl_sets[-1].hex()
assert u16(hsl_sets[-1], 4) == 120, hsl_sets[-1].hex()
assert u16(hsl_sets[-1], 6) == 100, hsl_sets[-1].hex()
assert len(sent(sim, 0x825E)) == before_ctl, "colour mode must not use CTL"
state.release()

# --- 3. HSL Status clamping cannot fake a 100% reading ---------------------
# Bulb 1's range is 1..50.  Sending 50% HSL means raw 25; its Status echoes
# 25, which is half the range, and brightness must stay 50%, not 100%.
state2 = make_state(root / "clamp")
state2.device(UNICAST).update(
    {"factory_range_max": 50, "last_level_pct": 50, "mode": "colour",
     "hue": 120, "saturation": 100}
)
state2.save()
link2 = meshcore.Link(state2, UNICAST)
sim2 = Sim((1, 50))
sim2.hsl = (25, 120, 100)  # the Status read-back
sim2.ll = 50
install_transport(link2, sim2)

st = asyncio.run(link2.status())
assert st["level_pct"] == 50, st
assert st["level"] == 25, st  # white-equivalent on the factory range
assert st["hue"] == 120, st

# And a level change still sends half of the 1..50 range for 100%.
asyncio.run(link2.set_level(50))
hsl_sets = sent(sim2, meshcore.OP_LIGHT_HSL_SET)
assert u16(hsl_sets[-1], 2) == 13, hsl_sets[-1].hex()
state2.release()

# --- 4. White-mode brightness is read from Light Lightness -----------------
state3 = make_state(root / "white")
state3.device(UNICAST).update(
    {"factory_range_max": 50, "last_level_pct": 100, "mode": "temperature"}
)
state3.save()
link3 = meshcore.Link(state3, UNICAST)
sim3 = Sim((1, 50))
sim3.ll = 5  # 10% of 1..50
sim3.ctl_lightness = 5
sim3.onoff = 1
install_transport(link3, sim3)

st = asyncio.run(link3.status())
assert st["level_pct"] == 10, st
assert st["level"] == 5, st
assert st["on"] is True, st
assert state3.device(UNICAST)["last_level_pct"] == 10
state3.release()

# --- 5. On/off comes from Generic OnOff ------------------------------------
state4 = make_state(root / "onoff")
state4.device(UNICAST).update(
    {"factory_range_max": 50, "last_level_pct": 30, "mode": "temperature"}
)
state4.save()
link4 = meshcore.Link(state4, UNICAST)
sim4 = Sim((1, 50))
sim4.ll = 15
sim4.onoff = 0
install_transport(link4, sim4)

st = asyncio.run(link4.status())
assert st["on"] is False, st
assert st["level_pct"] == 30, st  # lightness is retained while off

asyncio.run(link4.set_power(True))
onoff_sets = sent(sim4, 0x8202)
assert onoff_sets[-1][2] == 1, onoff_sets[-1].hex()
ctl_sets = sent(sim4, 0x825E)
assert u16(ctl_sets[-1], 2) == 15, ctl_sets[-1].hex()  # 30% of 1..50

asyncio.run(link4.set_power(False))
onoff_sets = sent(sim4, 0x8202)
assert onoff_sets[-1][2] == 0, onoff_sets[-1].hex()
state4.release()

# --- 6. An old widened range is restored to the factory maximum -----------
state5 = make_state(root / "restore")
state5.device(UNICAST).update(
    {"factory_range_max": 50, "last_level_pct": 10}
)
state5.save()
link5 = meshcore.Link(state5, UNICAST)
sim5 = Sim((1, 0xFFFF))
install_transport(link5, sim5)

mn, mx = asyncio.run(link5.ensure_lightness_range())
assert (mn, mx) == (1, 50), (mn, mx)
range_sets = sent(sim5, meshcore.OP_LIGHT_LIGHTNESS_RANGE_SET)
assert len(range_sets) == 1, range_sets
assert u16(range_sets[0], 2) == 1 and u16(range_sets[0], 4) == 50, (
    range_sets[0].hex()
)
assert sim5.range == (1, 50)
state5.release()

# A missing Setup Server is bound before the restore.
state6 = make_state(root / "rebind", [0x1000, 0x1300, 0x1303, 0x1307])
state6.device(UNICAST).update({"factory_range_max": 100})
state6.save()
link6 = meshcore.Link(state6, UNICAST)
node = FakeNode()
sim6 = Sim((1, 0xFFFF))
install_transport(link6, sim6, node=node)
mn, mx = asyncio.run(link6.ensure_lightness_range())
assert (mn, mx) == (1, 100), (mn, mx)
assert len(node.calls) == 1, node.calls
addr, payload = node.calls[0]
assert addr == UNICAST, hex(addr)
assert payload[:2] == meshcore.encode_opcode(0x803D)  # Config Model App Bind
assert 0x1301 in state6.device(UNICAST)["bound_models"]
state6.release()

# --- 7. Legacy raw levels migrate to percentages ---------------------------
def legacy(root: Path, last_level: int, level_range_max: int | None) -> dict:
    state = make_state(root)
    entry = state.device(UNICAST)
    entry["last_level"] = last_level
    if level_range_max is None:
        entry.pop("level_range_max", None)
    else:
        entry["level_range_max"] = level_range_max
    entry.pop("last_level_pct", None)
    entry.pop("factory_range_max", None)
    state.save()
    state._migrate()
    migrated = dict(state.device(UNICAST))
    state.release()
    return migrated


entry = legacy(root / "legacy-wide", 0x8000, 0xFFFF)
assert entry["last_level_pct"] == 50, entry
assert "last_level" not in entry and "level_range_max" not in entry, entry

entry = legacy(root / "legacy-bulb2", 10, 100)
assert entry["last_level_pct"] == 10, entry
assert entry["factory_range_max"] == 100, entry

entry = legacy(root / "legacy-bulb1", 50, 50)
assert entry["last_level_pct"] == 100, entry
assert entry["factory_range_max"] == 50, entry

entry = legacy(root / "legacy-25-of-50", 25, 50)
assert entry["last_level_pct"] == 50, entry

entry = legacy(root / "legacy-pre-range", 50, None)
assert entry["last_level_pct"] == 50, entry

# --- 8. Imported legacy fields are converted too ---------------------------
key, entry = meshcore._normalise_device(
    "0x0002",
    {
        "device_key": "11" * 16,
        "bound_models": [0x1300],
        "last_level": 10,
        "level_range_max": 100,
    },
)
assert key == "0x0002"
assert entry["last_level_pct"] == 10, entry
assert "last_level" not in entry and "level_range_max" not in entry, entry

print("colour-scale regression test passed")
