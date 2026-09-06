"""orchestratord-acp — generic ACP (Agent Client Protocol) backend.

A single backend class (:class:`AcpBackend`) drives multiple ACP-speaking
runtimes. The runtime identity (grok / codebuddy / qwenpaw / qodercli /
qoderclicn / deveco) is selected at construction via the descriptor's
``prefer`` hint (mirroring codex's ``codex-cli`` / ``codex-app-server``
split). See FEATURE_GAP_VS_MULTICA.md §8.3.
"""

from orchestratord_acp.backend import AcpBackend
from orchestratord_acp.runtime import AcpRuntime, resolve_runtime

__all__ = ["AcpBackend", "AcpRuntime", "resolve_runtime"]
