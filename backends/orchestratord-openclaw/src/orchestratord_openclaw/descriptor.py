"""openclaw backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

OPENCLAW_DESCRIPTOR = BackendDescriptor(
    name="openclaw",
    display_name="OpenClaw (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-openclaw",
    capabilities=frozenset({
        "parallel_sessions",
    }),
    cli_command="openclaw",
    env_prefix="OPENCLAW_",
    launch_header=(
        "openclaw (spawn-per-turn; §8.1 HTTP/Gateway path deferred)"
    ),
    model_discovery="user",
)
