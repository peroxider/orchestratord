"""zeroclaw backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

ZEROCLAW_DESCRIPTOR = BackendDescriptor(
    name="zeroclaw",
    display_name="ZeroClaw (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-zeroclaw",
    capabilities=frozenset({
        "parallel_sessions",
        "streaming_deltas",
    }),
    cli_command="zeroclaw",
    env_prefix="ZEROCLAW_",
    launch_header="zeroclaw (spawn-per-turn, ACP/JSON-RPC over stdio: §8.2.3)",
    model_discovery="user",
)
