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
    }),
    cli_command="zeroclaw",
    env_prefix="ZEROCLAW_",
    launch_header="zeroclaw (spawn-per-turn, event stream shape not yet exercised)",
    model_discovery="user",
)
