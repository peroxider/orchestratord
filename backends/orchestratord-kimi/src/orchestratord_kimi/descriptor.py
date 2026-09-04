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
    }),
    cli_command="kimi",
    env_prefix="KIMI_",
    launch_header="kimi (spawn-per-turn, zh-CN prompt friendly)",
    model_discovery="user",
)