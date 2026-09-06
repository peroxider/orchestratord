"""reasonix backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

REASONIX_DESCRIPTOR = BackendDescriptor(
    name="reasonix",
    display_name="Reasonix (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-reasonix",
    capabilities=frozenset({
        "parallel_sessions",
    }),
    cli_command="reasonix",
    env_prefix="REASONIX_",
    launch_header="reasonix (spawn-per-turn, event stream shape not yet exercised)",
    model_discovery="user",
)
