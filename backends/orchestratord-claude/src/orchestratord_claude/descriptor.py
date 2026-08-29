"""claude backend — descriptor declaration.

Single source of truth (DESIGN_two_tier_backend_registry.md §1.2 / §2.2):
``capabilities`` / ``family`` / ``cli_command`` live here; ``backend.py``
only carries behavior (construct :class:`AgentBackend`, produce SPI
capability instances).
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

CLAUDE_DESCRIPTOR = BackendDescriptor(
    name="claude",
    display_name="Claude Code (CLI: claude / ccb)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-claude",
    capabilities=frozenset({
        "streaming_deltas",
        "resumable",
        "parallel_sessions",
        "cost_reporting",
    }),
    cli_command=None,
    env_prefix="CLAUDE_",
    launch_header="Anthropic Claude Code CLI (per-turn spawn, stream-json)",
    model_discovery="user",
)
