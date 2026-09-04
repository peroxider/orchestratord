"""CommandRouter — map ``command`` semantic to existing entry points (P5).

Recognizes the orchestrator issue-CLI control verbs
(pause/resume/stop/inject/clarify/review/feedback/retry) and the agent
intent commands (``/agent retry|follow-up|unblock``). It also normalizes
the README-documented IM orchestrator commands (``/issue ...`` and
``/server status``) to the existing orchestrator CLI argv shape. It does
NOT invent new synonyms — same names as the existing surfaces, aligned
with ``orchestratord.im_gateway_client``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from orchestratord.ipc.models import InboundMessage

_AGENT_CMD_RE = re.compile(r"^\s*/agent\s+(retry|follow-up|unblock)\b[^\n]*", re.IGNORECASE)
_CONTROL_VERB_RE = re.compile(
    r"^\s*/(?P<verb>pause|resume|stop|takeover|inject|detach|clarify|review|feedback)\b[^\n]*",
    re.IGNORECASE,
)

# Loose issue-id matching (e.g. AGENTSDK-15, PROJ-128).
_ISSUE_ID_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

_ORCHESTRATOR_ISSUE_COMMANDS = frozenset(
    {
        "list",
        "show",
        "tail",
        "stop",
        "pause",
        "resume",
        "clarify",
        "inject",
        "feedback",
        "review",
        "retry",
        "workspace",
        "rebase",
    }
)


@dataclass
class CommandRoute:
    kind: str  # "agent_intent" | "control_verb" | "orchestrator_cli"
    verb: str  # retry | follow-up | unblock | pause | resume | ...
    issue_hint: str | None = None
    payload: str = ""
    argv: tuple[str, ...] = ()


class CommandRouter:
    def route(self, message: InboundMessage) -> CommandRoute | None:
        text = (message.text or "").strip()
        argv = self._orchestrator_argv(text)
        if argv is not None:
            return CommandRoute(
                kind="orchestrator_cli",
                verb=argv[0],
                issue_hint=self._extract_issue(text, argv),
                payload=text,
                argv=tuple(argv),
            )
        m = _AGENT_CMD_RE.match(text)
        if m:
            verb = m.group(1).lower()
            issue_hint = self._extract_issue(text)
            return CommandRoute(kind="agent_intent", verb=verb, issue_hint=issue_hint, payload=text)
        m = _CONTROL_VERB_RE.match(text)
        if m:
            verb = m.group("verb").lower()
            issue_hint = self._extract_issue(text)
            return CommandRoute(kind="control_verb", verb=verb, issue_hint=issue_hint, payload=text)
        return None

    @staticmethod
    def _orchestrator_argv(text: str) -> list[str] | None:
        tokens = _split_command_tokens(text)
        if not tokens:
            return None
        command = tokens[0].lower()
        if command == "issue" and len(tokens) >= 2:
            issue_subcommand = tokens[1].lower()
            if issue_subcommand in _ORCHESTRATOR_ISSUE_COMMANDS:
                return ["issue", issue_subcommand, *tokens[2:]]
        if command == "server" and len(tokens) >= 2 and tokens[1].lower() == "status":
            return ["server", "status", *tokens[2:]]
        return None

    @staticmethod
    def _extract_issue(text: str, argv: list[str] | None = None) -> str | None:
        if argv:
            for idx, token in enumerate(argv):
                if token == "--id" and idx + 1 < len(argv):
                    return argv[idx + 1]
                if token.startswith("--id="):
                    return token.split("=", 1)[1]
        m = _ISSUE_ID_RE.search(text)
        return m.group(1) if m else None


def _split_command_tokens(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return []
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return []
    tokens[0] = tokens[0].lstrip("/")
    return tokens


__all__ = ["CommandRoute", "CommandRouter"]
