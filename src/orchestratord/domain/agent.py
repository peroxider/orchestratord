"""Agent entity model (§6.2).

``Agent`` is the persisted twin of a runtime backend. Model-level
invariants:

* ``provider`` must be a supported type (``SupportedTypes`` analogue) — no
  orphan providers.
* ``capabilities_cache_jsonb`` must carry every core capability bit so the
  Web capability matrix can render without late validation.
* ``created_at`` is timezone-aware.

Uniqueness of ``(workspace_id, name)`` and cross-workspace reference
rejection are enforced at the repository layer (not here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

# The 8 core capability bits (§6.2 / spi.capabilities) the capability matrix
# renders. ``goal_mode`` and ``resume_detection`` are optional extras carried
# in the same cache but not required for the matrix.
CORE_CAPABILITY_BITS: tuple[str, ...] = (
    "streaming_deltas",
    "resumable",
    "interrupt",
    "approval_hooks",
    "parallel_sessions",
    "cost_reporting",
    "tool_filtering",
    "takeover",
)

# SupportedTypes analogue (docs/FEATURE_GAP_VS_MULTICA.md §14.1): the set of
# provider/backend identities the orchestrator knows how to drive. Static
# baseline mirrors ``scripts/agent-cli-command-names.txt`` plus the
# descriptor names; the live backend registry supplements it once backend
# packages are installed (Phase 2).
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {
        "claude",
        "clawcodex-dev",
        "codebuddy",
        "codex",
        "codex-cli",
        "codex-app-server",
        "copilot",
        "cursor",
        "cursor-agent",
        "deveco",
        "dsh",
        "grok",
        "hermes",
        "kimi",
        "kiro",
        "kiro-cli",
        "openclaw",
        "opencode",
        "qodercli",
        "qoderclicn",
        "qwen",
        "qwenpaw",
        "reasonix",
        "zeroclaw",
    }
)


@dataclass
class Agent:
    id: UUID
    workspace_id: UUID
    name: str
    provider: str
    runtime_id: UUID
    capabilities_cache_jsonb: dict
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.provider not in _SUPPORTED_PROVIDERS:
            raise ValueError(
                f"unknown provider {self.provider!r}; supported: "
                f"{sorted(_SUPPORTED_PROVIDERS)}"
            )
        missing = [
            bit
            for bit in CORE_CAPABILITY_BITS
            if bit not in self.capabilities_cache_jsonb
        ]
        if missing:
            raise ValueError(
                f"capabilities_cache_jsonb missing required capability "
                f"bits: {missing}"
            )
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
        elif self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=UTC)
