"""MessageClassifier — six-class inbound semantics (P5).

Classification rules (no natural-language auto-judgment for
``interrupt``/``contextOnly`` — those require structured metadata or
existing control/bridge entry points):

  1. structured ``deliverAs`` metadata → that semantic (explicit)
  2. explicit slash commands recognized by ``CommandRouter`` → ``command``
  3. plain text while session busy → ``followUp`` (queue-as-followUp)
  4. otherwise → ``newPrompt``

``approval`` is only set via structured metadata (``deliverAs=approval``
or a bound wait-point reply) — a bare "yes" is ``newPrompt`` unless
bound, to avoid misrouting approvals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestratord.ipc.models import InboundMessage, MessageSemantics

_COMMAND_ROUTER = None

_DELIVER_AS_MAP = {
    "newPrompt": "NEW_PROMPT",
    "command": "COMMAND",
    "followUp": "FOLLOW_UP",
    "approval": "APPROVAL",
    "interrupt": "INTERRUPT",
    "contextOnly": "CONTEXT_ONLY",
}


class MessageClassifier:
    def classify(
        self,
        message: InboundMessage,
        *,
        is_busy: bool = False,
        has_pending_wait: bool = False,
    ) -> MessageSemantics:
        MessageSemantics = _message_semantics()
        # 1. structured deliverAs wins (explicit, no NL guessing)
        deliver_as = self._deliver_as(message)
        if deliver_as is not None:
            return deliver_as
        # 2. explicit slash commands
        if _command_router().route(message) is not None:
            return MessageSemantics.COMMAND
        # 3. approval only via structured metadata or bound wait-point
        if has_pending_wait and message.semantic_tags and "approval" in message.semantic_tags:
            return MessageSemantics.APPROVAL
        # 4. busy ordinary text → queue-as-followUp
        if is_busy:
            return MessageSemantics.FOLLOW_UP
        # 5. default
        return MessageSemantics.NEW_PROMPT

    def _deliver_as(self, message: InboundMessage) -> MessageSemantics | None:
        MessageSemantics = _message_semantics()
        raw = None
        if message.raw and isinstance(message.raw, dict):
            raw = message.raw.get("deliverAs")
        if raw is None:
            # semantic_tags may carry an explicit semantic
            for tag in message.semantic_tags or []:
                if tag in _DELIVER_AS_MAP:
                    return getattr(MessageSemantics, _DELIVER_AS_MAP[tag])
        if isinstance(raw, str) and raw in _DELIVER_AS_MAP:
            return getattr(MessageSemantics, _DELIVER_AS_MAP[raw])
        return None


def _message_semantics():
    from orchestratord.ipc.models import MessageSemantics

    return MessageSemantics


def _command_router():
    global _COMMAND_ROUTER
    if _COMMAND_ROUTER is None:
        from orchestratord.im_gateway.semantics.command_router import CommandRouter

        _COMMAND_ROUTER = CommandRouter()
    return _COMMAND_ROUTER


__all__ = ["MessageClassifier"]
