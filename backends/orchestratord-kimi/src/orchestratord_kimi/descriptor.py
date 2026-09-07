"""kimi backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

KIMI_DESCRIPTOR = BackendDescriptor(
    name="kimi",
    display_name="Kimi (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-kimi",
    capabilities=frozenset({
        "parallel_sessions",
        "streaming_deltas",
    }),
    cli_command="kimi",
    env_prefix="KIMI_",
    launch_header="kimi acp (ACP JSON-RPC stdio, spawn-per-turn)",
    model_discovery="user",
)