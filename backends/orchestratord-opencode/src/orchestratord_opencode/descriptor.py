"""opencode backend — descriptor 声明。"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

OPENCODE_DESCRIPTOR = BackendDescriptor(
    name="opencode",
    display_name="OpenCode (Protocol)",
    family=BackendFamily.PROTOCOL,
    backend_package="orchestratord-opencode",
    capabilities=frozenset({
        "streaming_deltas",
        "approval_hooks",
        "parallel_sessions",
        # opencode serve exposes session/load HTTP probe
        # (see session.py:probe_resume).
        "resume_detection",
    }),
    cli_command=None,
    env_prefix="OPENCODE_",
    launch_header="opencode serve (HTTP/SSE)",
    model_discovery="user",
)