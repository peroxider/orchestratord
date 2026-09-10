"""Learnings generation (DESIGN_EXPERIENCE_LOOP.md §3 写侧 A).

The daemon has no wired LLM provider yet, so the first-cut generator is
mechanical: :class:`TemplateGenerator` assembles a structured document
from friction signals, feedback text, and the session's own output
tail. The :class:`LearningGenerator` protocol is the swap point for a
skill/LLM-backed generator once a SPI provider lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestratord.telemetry.friction import FrictionSignals

from .writer import LearningDoc

_OUTPUT_TAIL_CHARS = 2000


@dataclass
class SessionMaterial:
    session_id: str
    issue_id: str
    repo: str
    friction_score: int
    signals: FrictionSignals
    end_reason: str = ""
    backend: str = ""
    issue_title: str = ""
    feedback_items: list[Any] = field(default_factory=list)
    output_text: str = ""


class LearningGenerator(Protocol):
    def generate(self, material: SessionMaterial) -> LearningDoc: ...


def _feedback_bodies(items: list[Any]) -> list[str]:
    bodies: list[str] = []
    for item in items:
        body = getattr(item, "body", None) or (
            item.get("body") if isinstance(item, dict) else None
        )
        if body:
            bodies.append(str(body))
    return bodies


class TemplateGenerator:
    """Mechanical structured learning: 背景 / 坑 / 解法 / 标签."""

    def generate(self, material: SessionMaterial) -> LearningDoc:
        return LearningDoc(
            session_id=material.session_id,
            issue_id=material.issue_id,
            repo=material.repo,
            friction_score=material.friction_score,
            tags=self._tags(material),
            title=self._title(material),
            body=self._body(material),
        )

    def _tags(self, material: SessionMaterial) -> list[str]:
        tags: list[str] = []
        if material.signals.retry_count:
            tags.append("kind:retry")
        if material.signals.degradations:
            tags.append("kind:degradation")
        if material.signals.approvals:
            tags.append("kind:approval")
        if material.signals.errors:
            tags.append("kind:error")
        if material.backend:
            tags.append(f"backend:{material.backend}")
        if material.end_reason:
            tags.append(f"end:{material.end_reason}")
        # Design §8: force 3-5 topical tags so recall has an exact-match
        # path without a CJK tokenizer.
        for extra in ("experience", "session", "workflow"):
            if len(tags) >= 3:
                break
            if extra not in tags:
                tags.append(extra)
        return tags[:5]

    def _title(self, material: SessionMaterial) -> str:
        base = material.issue_title or material.issue_id or "session experience"
        return f"{base}（friction {material.friction_score}）"

    def _body(self, material: SessionMaterial) -> str:
        lines: list[str] = []
        lines.append("## 背景")
        lines.append(
            f"- session: {material.session_id}（issue {material.issue_id or 'n/a'}，"
            f"backend {material.backend or 'n/a'}，结束原因 {material.end_reason or 'n/a'}）"
        )
        lines.append(f"- friction 评分：{material.friction_score}/100")

        lines.append("\n## 坑")
        if material.signals.retry_count:
            reasons = "；".join(material.signals.retry_reasons[:5]) or "未记录"
            lines.append(f"- 重试 {material.signals.retry_count} 次：{reasons}")
        if material.signals.degradations:
            lines.append(f"- capability 降级 {material.signals.degradations} 次")
        if material.signals.approvals:
            lines.append(f"- 审批中断 {material.signals.approvals} 次")
        if material.signals.errors:
            lines.append("- 本次 session 出现错误")
        if not (
            material.signals.retry_count
            or material.signals.degradations
            or material.signals.approvals
            or material.signals.errors
        ):
            lines.append("- 无显式摩擦信号（时长离群触发）")

        feedback = _feedback_bodies(material.feedback_items)
        if feedback:
            lines.append("\n## 相关反馈")
            for body in feedback[:5]:
                lines.append(f"- {body.strip()[:300]}")

        tail = material.output_text.strip()[-_OUTPUT_TAIL_CHARS:]
        if tail:
            lines.append("\n## 处理摘录")
            lines.append(tail)
        return "\n".join(lines)


__all__ = ["LearningGenerator", "SessionMaterial", "TemplateGenerator"]
