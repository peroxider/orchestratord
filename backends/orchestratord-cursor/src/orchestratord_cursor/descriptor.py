"""cursor backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

CURSOR_DESCRIPTOR = BackendDescriptor(
    name="cursor",
    display_name="Cursor (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-cursor",
    capabilities=frozenset({
        "parallel_sessions",
    }),
    cli_command="cursor-agent",
    env_prefix="CURSOR_",
    launch_header="cursor-agent (spawn-per-turn)",
    model_discovery="user",
)