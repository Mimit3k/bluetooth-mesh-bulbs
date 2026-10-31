"""Offline checks for the retry after a freshly provisioned node times out.

No Bluetooth is involved: the provisioning handshake and the GATT link are
faked, so this exercises only :func:`meshcore.provision`'s retry loop and the
message it raises when every attempt fails.
"""

# ruff: noqa: E402  (the path bootstrap must run before imports)

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src", ROOT / "phase0"):
    sys.path.insert(0, str(_path))

import meshcore


class FakeLink:
    """Stand-in for meshcore.Link; only the context-manager API is used."""

    def __init__(self, *args, **kwargs) -> None:
        self.node = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


async def fake_p0_provision(args, data, path, transport) -> int:
    """Emulate phase0 writing the device it just provisioned."""
    data["devices"]["0x0002"] = {
        "device_key": "11" * 16,
        "uuid": "aa" * 16,
        "address": "AA:BB:CC:DD:EE:02",
    }
    data["next_unicast"] = 3
    return 2


async def fake_sleep(_seconds) -> None:
    """Skip the real 3 s / 6 s backoff."""


meshcore.Link = FakeLink
meshcore.p0.provision = fake_p0_provision
meshcore.p0.LocalTransport = lambda: object()
meshcore.asyncio.sleep = fake_sleep

# The first two configuration attempts time out, as they do while the node is
# rebooting after provisioning; the third succeeds.
calls: list[int] = []


async def flaky_configure(state, node, unicast, log_fn=print, **kwargs):
    calls.append(unicast)
    if len(calls) < 3:
        raise TimeoutError("no composition response")
    return [0x1000, 0x1300], object()


meshcore.configure_node = flaky_configure
with tempfile.TemporaryDirectory(prefix="bulbctl-retry-ok-") as tmp:
    state = meshcore.State(Path(tmp) / "state.json")
    try:
        unicast = asyncio.run(
            meshcore.provision(state, uuid="aa", name="Retry", log_fn=lambda _s: None)
        )
        assert unicast == 0x0002, hex(unicast)
        assert calls == [0x0002, 0x0002, 0x0002], calls
        assert state.device(0x0002)["bound_models"] == [0x1000, 0x1300]
    finally:
        state.release()


# When every attempt fails the bulb stays in the collection (its device key is
# real), and the error says how to retry without a factory reset.  A fresh
# provision gets five attempts, including the initial reboot delay.
fail_calls: list[int] = []


async def always_fail(state, node, unicast, log_fn=print, **kwargs):
    fail_calls.append(unicast)
    raise TimeoutError("no composition response")


meshcore.configure_node = always_fail
with tempfile.TemporaryDirectory(prefix="bulbctl-retry-fail-") as tmp:
    state = meshcore.State(Path(tmp) / "state.json")
    try:
        try:
            asyncio.run(
                meshcore.provision(state, uuid="bb", name="Nope", log_fn=lambda _s: None)
            )
        except meshcore.MeshCoreError as exc:
            text = str(exc)
            assert "provisioned at 0x0002" in text, text
            assert "already on the mesh" in text, text
            assert "bulbctl configure --device 0x0002" in text, text
        else:
            raise AssertionError("expected MeshCoreError after five failures")
        assert fail_calls == [0x0002] * 5, fail_calls
        assert state.device(0x0002)["bound_models"] == []
    finally:
        state.release()

print("post-provisioning retry tests passed")
