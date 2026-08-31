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
        # QueryRunner exposes probe_transcript (see
        # session.py:probe_resume). The orchestrator-side probe_resume
        # also falls back to a CLI ``--resume`` directory check via
        # ``clawcodex_ext.services.session_storage`` when the SDK probe
        # is unavailable (older clawcodex, broken install, etc.) — see
        # ClawcodexSession._probe_resume_via_storage.
        "resume_detection",
    }),
    cli_command=None,
    env_prefix="CLAWCODEX_",
    launch_header="Claw Codex agent (in-process SDK)",
    model_discovery="probe",
)