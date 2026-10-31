"""Offline checks for per-model AppKey binding retries.

Right after provisioning a lamp often drops the GATT proxy link part-way
through configuration.  The old code treated the resulting timeout as "model
unsupported" and silently saved a partially configured bulb, so only on/off
worked.  This suite fakes the node and the link and proves that a failed bind
reconnects and retries, that element-0 models are required, and that
secondary-element models stay best-effort.
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

ELEMENT_0_MODELS = [0x1000, 0x1300, 0x1303, 0x1304, 0x1307, 0x1308]


class FakeElement:
    def __init__(self, models):
        self.sig_models = models


class FakeComposition:
    def __init__(self, elements):
        self.elements = elements

    def describe(self) -> str:
        return "fake composition"


COMP = FakeComposition(
    [FakeElement(ELEMENT_0_MODELS), FakeElement([0x1306])]
)

meshcore.parse_config_appkey_status = lambda _raw: SimpleNamespace(status=0x00)
meshcore.parse_composition_data_status = lambda _raw: COMP
meshcore.parse_config_model_app_status = lambda _raw: SimpleNamespace(status=0x00)


def reply(opcode: int) -> SimpleNamespace:
    return SimpleNamespace(opcode=opcode, params=b"")


class FakeNode:
    """Answer config requests; optionally drop the first bind of a model."""

    def __init__(self, fail_once=None, always_fail=None):
        self.fail_once = dict(fail_once or {})
        self.always_fail = set(always_fail or ())
        self.added = {}
        self.successful_binds = []

    def add_device(self, address, key) -> None:
        self.added[address] = key

    async def request(self, dst, payload, opcode, **_kwargs):
        if opcode in (
            meshcore.OP_CONFIG_APPKEY_STATUS,
            meshcore.OP_CONFIG_COMPOSITION_DATA_STATUS,
        ):
            return reply(opcode)
        if opcode != meshcore.OP_CONFIG_MODEL_APP_STATUS:
            raise AssertionError(f"unexpected opcode {opcode:#06x}")
        model_id = int.from_bytes(payload[-2:], "little")
        if self.fail_once.get(model_id, 0) > 0:
            self.fail_once[model_id] -= 1
            raise TimeoutError(f"no 0x803e response for 0x{model_id:04x}")
        if model_id in self.always_fail:
            raise TimeoutError(f"no 0x803e response for 0x{model_id:04x}")
        self.successful_binds.append(model_id)
        return reply(opcode)


class FakeLink:
    node = None
    reconnects = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def reconnect(self) -> None:
        type(self).reconnects += 1
        self.node = FakeLink.node


def make_state(tmp: Path):
    state = meshcore.State(tmp / "state.json")
    state.add_device(
        0x000E,
        name="Desk",
        uuid="aa" * 16,
        address="AA:BB:CC:DD:EE:0E",
        device_key=bytes.fromhex("11" * 16),
        bound_models=[],
    )
    return state


# --- a dropped link on one bind reconnects and finishes the rest ---------
with tempfile.TemporaryDirectory(prefix="bulbctl-configure-retry-") as tmp:
    state = make_state(Path(tmp))
    try:
        node = FakeNode(fail_once={0x1300: 1})
        link = FakeLink()
        link.node = node
        FakeLink.node = node
        FakeLink.reconnects = 0

        bound, comp = asyncio.run(
            meshcore.configure_node(
                state, node, 0x000E, log_fn=lambda _s: None, link=link
            )
        )
        assert comp is COMP
        assert bound == ELEMENT_0_MODELS, [hex(m) for m in bound]
        assert node.successful_binds == ELEMENT_0_MODELS
        assert FakeLink.reconnects == 1, FakeLink.reconnects
        # Device keys are re-registered for every element after a reconnect.
        assert set(link.node.added) == {0x000E, 0x000F}, [
            hex(k) for k in link.node.added
        ]
        assert all(k == bytes.fromhex("11" * 16) for k in link.node.added.values())
    finally:
        state.release()


# --- a required (element 0) model that never binds fails the pass --------
with tempfile.TemporaryDirectory(prefix="bulbctl-configure-required-") as tmp:
    state = make_state(Path(tmp))
    try:
        node = FakeNode(always_fail={0x1303})
        link = FakeLink()
        link.node = node
        FakeLink.node = node
        FakeLink.reconnects = 0
        try:
            asyncio.run(
                meshcore.configure_node(
                    state, node, 0x000E, log_fn=lambda _s: None, link=link
                )
            )
        except meshcore.MeshCoreError as exc:
            text = str(exc)
            assert "could not bind required model" in text, text
            assert "0x1303" in text, text
        else:
            raise AssertionError("a required element-0 model must not be skipped")
        assert FakeLink.reconnects == 1, FakeLink.reconnects
    finally:
        state.release()


# --- secondary-element models are skipped, never probed ---------------
with tempfile.TemporaryDirectory(prefix="bulbctl-configure-secondary-") as tmp:
    state = make_state(Path(tmp))
    try:
        node = FakeNode(always_fail={0x1306})
        link = FakeLink()
        link.node = node
        FakeLink.node = node
        FakeLink.reconnects = 0
        bound, _comp = asyncio.run(
            meshcore.configure_node(
                state, node, 0x000E, log_fn=lambda _s: None, link=link
            )
        )
        assert bound == ELEMENT_0_MODELS, [hex(m) for m in bound]
        assert 0x1306 not in node.successful_binds
        assert FakeLink.reconnects == 0, FakeLink.reconnects
    finally:
        state.release()

print("configuration retry tests passed")
