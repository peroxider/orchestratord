"""Standalone repro for test_mount_gateway_retries_initial_register_failure."""

import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, "src")

from orchestratord.cli import server as server_mod  # noqa: E402

calls: list[str] = []


class _FakeIpc:
    def __init__(self, sock, instance_id=None):
        self.sock = sock
        self.instance_id = instance_id
        self.on_deliver = None

    async def reconnect_until_registered(self, *, session_id, origin, capabilities, **_kwargs):
        calls.append(origin)
        if len(calls) == 1:
            return None
        return SimpleNamespace(ack_layer="accepted")

    async def heartbeat(self):
        raise asyncio.CancelledError()


class _FakeClient:
    def __init__(self, handlers, *, ipc_client=None, origin="", **_kwargs):
        self._ipc = ipc_client
        self._origin = origin

    async def send_outbound(self, text):
        return None


from orchestratord.ipc import client as ipc_client_mod  # noqa: E402
from orchestratord import im_gateway_client as igc  # noqa: E402

orig_ipc = ipc_client_mod.GatewayIpcClient
orig_client = igc.OrchestratorGatewayClient
ipc_client_mod.GatewayIpcClient = _FakeIpc
igc.OrchestratorGatewayClient = _FakeClient

_real_sleep = asyncio.sleep


async def _fast_sleep(_delay):
    await _real_sleep(0)


orig_sleep = asyncio.sleep
asyncio.sleep = _fast_sleep

print("server_mod.asyncio is asyncio:", server_mod.asyncio is asyncio, flush=True)


async def _run():
    return None


subsystem = SimpleNamespace(_orchestrator=None, run=_run)
config = SimpleNamespace(workspace=SimpleNamespace(root="/repo"))
wrapper = server_mod._mount_gateway_opt_in(
    subsystem, config, enabled=True, origin=None, sock="/tmp/gateway.sock"
)

try:
    asyncio.run(wrapper._heartbeat_loop())
    print("OK calls=", calls, flush=True)
except BaseException as e:  # noqa: BLE001
    print("EXC", type(e).__name__, repr(e), "calls=", calls, flush=True)
finally:
    ipc_client_mod.GatewayIpcClient = orig_ipc
    igc.OrchestratorGatewayClient = orig_client
    asyncio.sleep = orig_sleep
