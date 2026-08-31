"""Issue-to-PR application boundary.

The historical ``OrchestrationSubsystem`` remains import-compatible, while
new composition roots should use this business-specific name.
"""

from __future__ import annotations

from orchestratord.orchestration_subsystem import OrchestrationSubsystem


class IssueToPrApplication(OrchestrationSubsystem):
    """Poll issues, execute configured workflows, and synchronize PRs."""
