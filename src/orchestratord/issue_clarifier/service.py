"""LLM-backed static issue clarity analysis."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .cache import ClarifierCache, build_fingerprint
from .models import ClarifyQuestion, ClarifyResult
from .parser import parse_clarify_response
from .prompt import build_clarify_messages

if TYPE_CHECKING:
    from ..issue import Issue

logger = logging.getLogger(__name__)

_EXPLICIT_GAP_PATTERNS = (
    re.compile(r"\b(?:intentionally|deliberately)\s+(?:left\s+)?unspecified\b", re.I),
    re.compile(r"\bTBD\b", re.I),
    re.compile(r"未指定|待定|尚未确定"),
)
_DO_NOT_GUESS_PATTERN = re.compile(r"\bdo\s+not\s+guess\b|不要猜", re.I)
_ASK_AUTHOR_PATTERN = re.compile(r"\bask\s+(?:the\s+)?(?:issue\s+)?author\b|询问作者|向作者确认", re.I)


class IssueClarifierService:
    def __init__(
        self,
        *,
        config: Any,
        cache: ClarifierCache,
        provider: Any | None = None,
        provider_factory: Callable[[], Any] | None = None,
        model: str | None = None,
    ) -> None:
        self.config = config
        self.cache = cache
        self._provider = provider
        self._provider_factory = provider_factory
        self.model = model

    def fingerprint(self, issue: "Issue", *, prior_replies: Iterable[str] = ()) -> str:
        return build_fingerprint(issue, prior_replies=prior_replies)

    def analyze(
        self,
        issue: "Issue",
        *,
        prior_replies: Iterable[str] = (),
        workspace_focuses: list[dict] | None = None,  # ★ P2
    ) -> ClarifyResult:
        replies = tuple(str(reply) for reply in prior_replies if str(reply).strip())
        fingerprint = self.fingerprint(issue, prior_replies=replies)
        cached = self.cache.get(fingerprint)
        if cached is not None:
            return cached

        explicit_gap = _find_explicit_clarification_gap(issue, replies)
        if explicit_gap is not None:
            result = ClarifyResult(
                is_clear=False,
                ambiguities=(
                    ClarifyQuestion(
                        question=(
                            "The issue explicitly leaves required implementation details "
                            "open. What exact contract should be implemented?"
                        ),
                        ambiguity_type="missing",
                        evidence=explicit_gap,
                    ),
                ),
                confidence=1.0,
                fingerprint=fingerprint,
                reason="explicit clarification directive in issue text",
                metadata={"deterministic_gate": "explicit_gap"},
            )
            self.cache.put(result)
            return result

        try:
            provider = self._get_provider()
            if provider is None:
                raise RuntimeError("clarifier provider is unavailable")
            messages = build_clarify_messages(
                issue,
                prior_replies=replies,
                max_questions=self.config.max_questions,
                max_input_tokens=self.config.max_input_tokens,
                workspace_focuses=workspace_focuses,  # ★ P2
            )
            try:
                response = provider.chat(
                    messages=messages,
                    tools=None,
                    model=self.model,
                    max_tokens=self.config.max_output_tokens,
                )
            except TypeError:
                response = provider.chat(messages=messages, tools=None, model=self.model)
            raw = str(getattr(response, "content", "") or "")
            result = parse_clarify_response(
                raw,
                min_confidence=self.config.min_confidence,
                max_questions=self.config.max_questions,
            ).with_runtime_fields(fingerprint=fingerprint)
        except Exception as exc:
            logger.warning("Issue clarifier failed open for issue %s: %s", issue.id, exc)
            result = ClarifyResult(
                is_clear=bool(self.config.fail_open),
                confidence=0.0,
                fingerprint=fingerprint,
                reason=f"clarifier unavailable: {type(exc).__name__}",
                degraded=True,
            )

        self.cache.put(result)
        return result

    def _get_provider(self) -> Any | None:
        if self._provider is None and self._provider_factory is not None:
            self._provider = self._provider_factory()
        return self._provider


def _find_explicit_clarification_gap(
    issue: "Issue",
    replies: tuple[str, ...],
) -> str | None:
    """Find an author-declared implementation gap before consulting an LLM."""
    if replies:
        return None
    text = "\n".join(
        value
        for value in (
            str(getattr(issue, "title", "") or ""),
            str(getattr(issue, "description", "") or ""),
        )
        if value
    )
    for pattern in _EXPLICIT_GAP_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    do_not_guess = _DO_NOT_GUESS_PATTERN.search(text)
    ask_author = _ASK_AUTHOR_PATTERN.search(text)
    if do_not_guess is not None and ask_author is not None:
        return f"{do_not_guess.group(0)}; {ask_author.group(0)}"
    return None


def format_clarification_request(result: ClarifyResult) -> tuple[str, list[str]]:
    lines = ["Before automated implementation can start, please clarify:"]
    flat_options: list[str] = []
    for index, ambiguity in enumerate(result.ambiguities, start=1):
        lines.append(f"{index}. {ambiguity.question}")
        if ambiguity.evidence:
            lines.append(f"   Context: {ambiguity.evidence}")
        for option in ambiguity.suggested_options:
            lines.append(f"   - {option}")
            flat_options.append(option)
    return "\n".join(lines), flat_options


__all__ = ["IssueClarifierService", "format_clarification_request"]
