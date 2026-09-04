"""copilot backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

COPILOT_DESCRIPTOR = BackendDescriptor(
    name="copilot",
    display_name="GitHub Copilot (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-copilot",
    capabilities=frozenset({
        "parallel_sessions",
    }),
    cli_command="copilot",
    env_prefix="COPILOT_",
    launch_header="copilot (spawn-per-turn, experimental stream shape)",
    model_discovery="user",
)