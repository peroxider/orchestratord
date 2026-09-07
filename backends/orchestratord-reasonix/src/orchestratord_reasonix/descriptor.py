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
        "streaming_deltas",
    }),
    cli_command="reasonix",
    env_prefix="REASONIX_",
    launch_header=(
        "reasonix acp --profile balanced --planner auto "
        "--sandbox-network auto --sandbox-bash auto --workspace-only "
        "(ACP JSON-RPC stdio, spawn-per-turn)"
    ),
    model_discovery="user",
)
