"""Issue-to-PR application（DESIGN §4.2 / §6，P4 空壳转正）。

The historical ``OrchestrationSubsystem`` remains import-compatible, while
new composition roots should use this business-specific name.

P4 余量将按 Application 协议（prepare_run/interpret_result/commands/
work_provider/prompt_profiles）逐步把 orchestrator.py 的业务段
（_launch_issue/_run_issue、rebase/review-followup/retry 的
SPAWN/RETRY Outcome）重组进本包。
"""

from __future__ import annotations

from orchestratord.orchestration_subsystem import OrchestrationSubsystem


class IssueToPrApplication(OrchestrationSubsystem):
    """Poll issues, execute configured workflows, and synchronize PRs."""
