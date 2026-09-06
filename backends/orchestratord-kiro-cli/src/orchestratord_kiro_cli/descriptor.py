"""kiro-cli backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

KIRO_DESCRIPTOR = BackendDescriptor(
    name="kiro-cli",
    display_name="AWS Kiro (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-kiro-cli",
    capabilities=frozenset({
        "parallel_sessions",
    }),
    cli_command="kiro",
    env_prefix="KIRO_",
    launch_header="kiro (spawn-per-turn, event stream shape not yet exercised)",
    model_discovery="user",
)
