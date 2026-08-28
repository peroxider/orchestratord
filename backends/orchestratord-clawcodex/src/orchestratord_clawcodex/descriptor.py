"""clawcodex backend — descriptor 声明。

单一真源（DESIGN_two_tier_backend_registry.md §1.2 / §2.2）：
``capabilities`` / ``family`` 等元数据集中在此处；``backend.py`` 仅负责
行为（构造 :class:`AgentBackend`、产出 SPI capability 实例）。
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

CLAWCODEX_DEV_DESCRIPTOR = BackendDescriptor(
    name="clawcodex-dev",
    display_name="Claw Codex",
    family=BackendFamily.IN_PROCESS,
    backend_package="orchestratord-clawcodex",
    capabilities=frozenset({
        "streaming_deltas",
        "approval_hooks",
        "cost_reporting",
        "tool_filtering",
        "takeover",
        "goal_mode",
        # ADR-003: QueryRunner exposes probe_transcript (see session.py).
        "resume_detection",
    }),
    cli_command=None,
    env_prefix="CLAWCODEX_",
    launch_header="Claw Codex agent (in-process SDK)",
    model_discovery="probe",
)