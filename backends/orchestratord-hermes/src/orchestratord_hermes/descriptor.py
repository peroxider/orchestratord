"""hermes backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

HERMES_DESCRIPTOR = BackendDescriptor(
    name="hermes",
    display_name="Hermes (Cli/venv)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-hermes",
    capabilities=frozenset({
        "resumable",
        "parallel_sessions",
    }),
    cli_command=None,
    env_prefix="HERMES_",
    launch_header="Hermes Python agent (per-turn spawn)",
    model_discovery="user",
)