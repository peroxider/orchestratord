"""Rule extraction, storage, and retrieval from PR review feedback.

Pipeline:
  - RuleStore: read/write workflow.rules.yaml with atomic write
  - RuleEngine.extract(): parse agent reply for ## Extracted Rules section
  - BatchedLLMJudge: batched LLM-based dedup / merge / conflict detection
    (an optional backend provider), replacing the earlier TF-IDF/embedding
    approach
  - RuleEngine.score(): 5-dimension quality scoring
  - RuleEngine.prune(): auto-prune when over max_rules limit
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RuleJudge protocol — batched LLM-based dedup / merge / conflict detection
# ---------------------------------------------------------------------------


@dataclass
class JudgeResult:
    """LLM 对单个 candidate 规则的去重/合并/冲突判定结果。"""

    action: Literal["duplicate", "merge", "conflict", "new"]
    target_idx: int | None = None
    """当 action 为 duplicate/merge/conflict 时，指向 existing 中对应的索引。"""


# ---------------------------------------------------------------------------
# ExtractTracker — 追踪已提取规则的 commit，保证幂等性
# ---------------------------------------------------------------------------


class ExtractTracker:
    """管理 ``.orchestratord_extracted.json``，记录已提取的 commit SHA。

    与 ``workflow.rules.yaml`` 同级存放，确保每次 ``extract``
    命令不会重复提取同一个 commit。
    """

    FILENAME = ".orchestratord_extracted.json"

    def __init__(self, rules_path: str) -> None:
        self._path = Path(rules_path).parent / self.FILENAME

    def load(self) -> set[str]:
        """读取已处理的 commit SHA 集合。"""
        if not self._path.exists():
            return set()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return set(data.get("processed_commits", []))
        except Exception:
            return set()

    def save(self, processed: set[str]) -> None:
        """写入已处理的 commit SHA 集合。"""
        data = {
            "version": 1,
            "processed_commits": sorted(processed),
            "last_extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# BatchedLLMJudge — 生产实现
# ---------------------------------------------------------------------------


class BatchedLLMJudge:
    """Batch-judge candidate rules against existing rules.

    Original implementation delegated LLM dedup to a concrete backend.
    The orchestrator must run independently of any backend, so this class
    falls back to a deterministic
    string-similarity judge when no SPI LLM provider is configured.

    The deterministic judge uses SequenceMatcher ratio on summary
    strings — high-ratio matches are flagged as DUPLICATE,
    medium-ratio as MERGE candidates, and below threshold as NEW.
    This is a deliberate fallback; the SPI LLM judge will be
    reimplemented in a follow-up.
    """

    _JUDGE_SYSTEM = (
        "You are a coding-convention analysis assistant. "
        "Given a set of existing rules and one or more new candidate rules, "
        "determine for each candidate whether it duplicates, should be merged "
        "with, semantically conflicts with, or is entirely new relative to the "
        "existing rules.  Reply ONLY with the exact format shown."
    )

    def __init__(self, provider_name: str | None = None, model: str | None = None) -> None:
        self._provider_name = provider_name
        self._model = model

    async def judge(self, candidates: list[dict], existing: list[dict]) -> list[JudgeResult]:
        if not candidates or not existing:
            return [JudgeResult(action="new") for _ in candidates]
        return self._judge_deterministic(candidates, existing)

    # ------------------------------------------------------------------
    # Deterministic fallback (no LLM, no subprocess)
    # ------------------------------------------------------------------

    # Similarity thresholds for the deterministic fallback.
    _DUP_THRESHOLD = 0.85
    _MERGE_THRESHOLD = 0.55

    def _judge_deterministic(
        self, candidates: list[dict], existing: list[dict],
    ) -> list[JudgeResult]:
        """String-similarity dedup — replaces the external provider call.

        For each candidate, compute SequenceMatcher ratio against every
        existing rule.  Highest ratio drives the action:

        * ``>= _DUP_THRESHOLD`` → DUPLICATE
        * ``>= _MERGE_THRESHOLD`` → MERGE (or CONFLICT if category differs)
        * otherwise → NEW
        """
        from difflib import SequenceMatcher

        results: list[JudgeResult] = []
        for cand in candidates:
            cand_summary = (cand.get("summary") or "").strip()
            cand_category = (cand.get("category") or "").strip()
            if not cand_summary:
                results.append(JudgeResult(action="new"))
                continue
            best_ratio = 0.0
            best_idx: int | None = None
            best_match: dict | None = None
            for i, ex in enumerate(existing):
                ex_summary = (ex.get("summary") or "").strip()
                ratio = SequenceMatcher(
                    a=cand_summary.lower(),
                    b=ex_summary.lower(),
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = i
                    best_match = ex
            if best_match is None or best_idx is None:
                results.append(JudgeResult(action="new"))
                continue
            if best_ratio >= self._DUP_THRESHOLD:
                results.append(
                    JudgeResult(action="duplicate", target_idx=best_idx)
                )
            elif best_ratio >= self._MERGE_THRESHOLD:
                ex_category = (best_match.get("category") or "").strip()
                action = "conflict" if cand_category != ex_category else "merge"
                results.append(
                    JudgeResult(action=action, target_idx=best_idx)
                )
            else:
                results.append(JudgeResult(action="new"))
        return results

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(candidates: list[dict], existing: list[dict]) -> str:
        lines: list[str] = ["Existing rules:"]
        for i, e in enumerate(existing, start=1):
            lines.append(f"{i}. [{e.get('category', '?')}] {e.get('summary', '')}")
            body = (e.get("body") or "").strip()
            if body:
                truncated = body[:120] + "..." if len(body) > 120 else body
                lines.append(f"   Body: {truncated}")

        lines.append("")
        lines.append("Candidate rules:")
        for ci, c in enumerate(candidates):
            label = chr(65 + ci)
            lines.append(f"{label}. [{c.get('category', '?')}] {c.get('summary', '')}")
            body = (c.get("body") or "").strip()
            if body:
                truncated = body[:120] + "..." if len(body) > 120 else body
                lines.append(f"   Body: {truncated}")

        lines.append("")
        lines.append("For each candidate reply EXACTLY one line in this format:")
        lines.append("A: DUPLICATE <existing_id>")
        lines.append("A: MERGE <existing_id>")
        lines.append("A: CONFLICT <existing_id>")
        lines.append("A: NEW")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Reply parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_reply(reply: str, num_candidates: int) -> list[JudgeResult]:
        results: list[JudgeResult] = []
        reply_lower = reply.strip().lower()

        for ci in range(num_candidates):
            label = chr(65 + ci)
            pattern = re.compile(
                rf"{label}\s*:\s*(duplicate|merge|conflict|new)\s*(\d+)?",
                re.IGNORECASE,
            )
            match = pattern.search(reply_lower)
            if match:
                action = match.group(1)
                target_str = match.group(2)
                target_idx = int(target_str) - 1 if target_str is not None else None
                results.append(JudgeResult(action=action, target_idx=target_idx))  # type: ignore[arg-type]
            else:
                logger.warning(
                    "Failed to parse judge result for candidate %s, defaulting to new", label
                )
                results.append(JudgeResult(action="new"))

        return results


# Regex to find the ## Extracted Rules section in an agent reply.
_RULES_SECTION_RE = re.compile(
    r"^##\s+Extracted\s+Rules\s*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.MULTILINE,
)

# Regex to parse individual rule items inside the section — strict form.
# Matches the canonical template format: `- [category] summary` + optional
# `Body: ...` on the next indented line. Kept for backward compatibility
# with the prompt template's documented format.
#
# The lookahead accepts the start of ANY next list item (strict `[cat]`,
# loose `**bold**`, or bare `\w` summary) so a strict item does not
# greedily swallow a following loose-format item (regex fix).
#
# The Body capture tolerates an optional leading list marker (`- ` or
# `* `) since LLMs frequently write `  - Body: ...` instead of `  Body:`
# (regex fix).
_RULE_ITEM_RE = re.compile(
    r"^\s*[-*]\s+\[([^\]]+)\]\s+(.+?)(?:\n\s*[-*]?\s*Body:\s*(.+?))?"
    r"(?=\n\s*[-*]\s+(?:\[|\*\*|\w)|\n\s*$|\Z)",
    re.DOTALL | re.MULTILINE,
)

# loose fallback regex for LLM output that deviates from the
# template. Tolerates three common deviations observed in practice:
#   (a) bold title instead of `[category]` — `- **Quote Style** — desc`
#   (b) Body line with a leading list marker — `  - Body: ...`
#   (c) bare summary with no category / title — `- Use double quotes`
# The category capture group is empty when no `[category]` prefix is
# present; `_infer_category()` then maps summary/body keywords to a
# canonical category (or falls back to `other`).
# Group layout: (1)=category-or-empty (2)=summary (3)=body-or-None
_RULE_ITEM_LOOSE_RE = re.compile(
    r"^\s*[-*]\s+(?:\[([^\]]*)\]\s+|\*\*([^*]*)\*\*\s*[-\u2014]\s+)?(.+?)"
    r"(?:\n\s*[-*]?\s*Body:\s*(.+?))?"
    r"(?=\n\s*[-*]\s+(?:\[|\*\*|\w)|\n\s*$|\Z)",
    re.DOTALL | re.MULTILINE,
)

# canonical category enum. Used by `_infer_category()` to map
# free-form summary/body text to a category when the agent omits `[cat]`.
_RULE_CATEGORIES = (
    "naming",
    "error_handling",
    "testing",
    "import_style",
    "code_style",
    "type_annotation",
    "architecture",
    "boilerplate",
    "security",
    "performance",
    "other",
)

# Keyword → category inference map (checked in order; first hit wins).
# Keys are lowercased substrings; a rule whose summary or body contains
# any keyword is assigned the corresponding category.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("error_handling", ("except", "exception", "error", "raise", "try", "catch", "异常")),
    ("testing", ("test", "pytest", "assert", "fixture", "mock", "测试")),
    ("import_style", ("import", "导入", "排序")),
    ("type_annotation", ("type", "annotation", "typing", "mypy", "类型")),
    ("naming", ("name", "naming", "variable", "function", "class", "命名")),
    ("architecture", ("layer", "module", "depend", "分层", "依赖", "架构")),
    ("boilerplate", ("license", "header", "docstring", "doc", "注释", "版权")),
    ("security", ("security", "auth", "token", "secret", "安全", "密钥")),
    ("performance", ("perf", "performance", "speed", "cache", "性能", "缓存")),
    (
        "code_style",
        (
            "quote",
            "引号",
            "双引号",
            "单引号",
            "indent",
            "缩进",
            "space",
            "空格",
            "format",
            "格式",
            "style",
            "风格",
            "bracket",
            "括号",
        ),
    ),
]


def _infer_category(text: str) -> str:
    """Map free-form rule text to a canonical category by keyword match.

    Returns the first matching category from ``_CATEGORY_KEYWORDS``, or
    ``'other'`` if no keyword hits. Used when the agent omits the
    ``[category]`` prefix (fix for loose LLM output).
    """
    lowered = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw.lower() in lowered for kw in keywords):
            return category
    return "other"


_AUTO_MANAGED_COMMENT = (
    "# workflow.rules.yaml \u2014 \u7531 orchestratord \u81ea\u52a8\u7ba1\u7406\n"
    "# \u89c4\u5219\u662f\u4ece PR review feedback \u4e2d\u5f52\u7eb3\u51fa\u7684\u53c2\u8003\u7ea6\u5b9a\uff0c\u975e\u5f3a\u5236\u7ea6\u675f\u3002\n"
    "# \u6ce8\u610f\uff1a\u6b64\u6587\u4ef6\u4ec5\u5728\u89c4\u5219\u5b66\u4e60\u542f\u7528\u5e76\u6210\u529f\u8fd0\u884c\u540e\u624d\u4f1a\u751f\u6210\uff1b\u82e5\u4e0d\u5b58\u5728\uff0c\u8bf7\u52ff\u4e3b\u52a8\u67e5\u627e\u3002"
)

# Default quality-score weights.
_W_SUPPORT = 0.30
_W_SPECIFICITY = 0.25
_W_RECENCY = 0.10
_W_AUTHORITY = 0.15
_W_CRITICALITY = 0.20


# ---------------------------------------------------------------------------
# RuleStore
# ---------------------------------------------------------------------------


class RuleStore:
    """Read/write ``workflow.rules.yaml`` with atomic write safety."""

    DEFAULT_FILENAME = "workflow.rules.yaml"

    @staticmethod
    def resolve_path(workflow_path: str, rules_path: str) -> str:
        if rules_path:
            p = Path(rules_path)
            if p.is_absolute():
                return str(p)
            return str((Path(workflow_path).parent / rules_path).resolve())
        return str((Path(workflow_path).parent / RuleStore.DEFAULT_FILENAME).resolve())

    @staticmethod
    def load(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {"version": 1, "rules": []}
        try:
            raw = p.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                return {"version": 1, "rules": []}
            data.setdefault("version", 1)
            data.setdefault("rules", [])
            return data
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("Failed to load rules file %s: %s", path, exc)
            return {"version": 1, "rules": []}

    @staticmethod
    def save(path: str, rules: list[dict], version: int = 1) -> None:
        p = Path(path)
        content = yaml.dump(
            {"version": version, "rules": rules},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        full_content = f"{_AUTO_MANAGED_COMMENT}\n{content}"
        tmp = p.with_suffix(".yaml.tmp")
        try:
            tmp.write_text(full_content, encoding="utf-8")
            tmp.replace(p)
        except OSError as exc:
            logger.error("Failed to save rules file %s: %s", path, exc)
            raise

    @staticmethod
    def is_user_managed(path: str) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        try:
            first = p.read_text(encoding="utf-8").splitlines()[0] if p.stat().st_size > 0 else ""
            # 只检测首行（兼容多行 header 的扩展）
            marker = _AUTO_MANAGED_COMMENT.splitlines()[0]
            return marker not in first
        except (OSError, IndexError):
            return False

    @staticmethod
    def ensure_file(path: str) -> None:
        p = Path(path)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        RuleStore.save(path, [], version=1)


# ---------------------------------------------------------------------------
# RuleEngine
# ---------------------------------------------------------------------------


class RuleEngine:
    """Rule extraction, deduplication, merge, quality scoring, pruning."""

    def __init__(self, store: RuleStore | None = None) -> None:
        self.store = store or RuleStore()

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract(agent_reply: str) -> list[dict]:
        match = _RULES_SECTION_RE.search(agent_reply)
        if not match:
            return []
        section = match.group(1).strip()
        rules: list[dict] = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Track char spans already consumed by the strict regex so the
        # loose fallback does not double-count the same rule. Spans are
        # half-open [start, end) over the stripped section text.
        strict_spans: list[tuple[int, int]] = []

        for m in _RULE_ITEM_RE.finditer(section):
            category = m.group(1).strip()
            summary = m.group(2).strip()
            body = m.group(3).strip() if m.group(3) else ""
            if not body and ":" in summary:
                parts = summary.split(":", 1)
                summary = parts[0].strip()
                body = parts[1].strip()
            rules.append(
                {
                    "category": category,
                    "summary": summary,
                    "body": body,
                    "source": "",
                    "support_count": 1,
                    "confidence": "medium",
                    "created_at": now,
                    "updated_at": now,
                    "conflict_with": [],
                    "last_applied": now,
                }
            )
            strict_spans.append((m.start(), m.end()))

        # loose fallback for LLM output that deviates from the
        # canonical `- [category] summary` template. Common deviations:
        # bold-title instead of `[cat]`, Body line with a leading `- `,
        # or a bare summary with no category prefix at all. We skip any
        # loose match whose start falls inside a strict span to avoid
        # duplicating rules the strict pass already captured.
        def _in_strict_span(pos: int) -> bool:
            for s, e in strict_spans:
                if s <= pos < e:
                    return True
            return False

        for m in _RULE_ITEM_LOOSE_RE.finditer(section):
            if _in_strict_span(m.start()):
                continue
            # Loose group layout: (1)=category-or-None (2)=bold-title-or-None
            # (3)=summary (4)=body-or-None. Exactly one of (1)/(2) is set.
            category = (m.group(1) or "").strip()
            bold_title = (m.group(2) or "").strip()
            summary = (m.group(3) or "").strip()
            body = (m.group(4).strip() if m.group(4) else "").strip()
            if not summary:
                continue
            # If the agent used a bold title instead of [category], fold
            # the title into the summary (it is usually the rule name).
            if bold_title and not category:
                summary = f"{bold_title} — {summary}" if summary else bold_title
            if not body and ":" in summary:
                parts = summary.split(":", 1)
                summary = parts[0].strip()
                body = parts[1].strip()
            if not category or category not in _RULE_CATEGORIES:
                category = _infer_category(f"{summary} {body}")
            rules.append(
                {
                    "category": category,
                    "summary": summary,
                    "body": body,
                    "source": "",
                    "support_count": 1,
                    "confidence": "medium",
                    "created_at": now,
                    "updated_at": now,
                    "conflict_with": [],
                    "last_applied": now,
                }
            )
        return rules

    # ------------------------------------------------------------------
    # Semantic dedup + merge (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_text(r: dict) -> str:
        """Join summary + body for similarity comparison."""
        parts = [r.get("summary", "")]
        if r.get("body"):
            parts.append(r["body"])
        return " ".join(parts)

    @staticmethod
    def _deduplicate_and_merge(
        candidates: list[dict],
        existing: list[dict],
        _judge_results: list[JudgeResult] | None = None,
    ) -> list[dict]:
        """Phase 2 dedup + merge.

        When ``_judge_results`` is provided (LLM path), its decisions
        override the default all-new behaviour.  Exact dedup (same
        summary text, case-insensitive) is always applied as a fast
        path regardless of the judge.
        """
        # --- fast path: exact dedup ---------------------------------------------------
        merged = list(existing)
        existing_map: dict[str, dict] = {}
        for r in merged:
            key = r.get("summary", "").strip().lower()
            if key:
                existing_map[key] = r

        remaining: list[tuple[int, dict]] = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for oi, c in enumerate(candidates):
            key = c.get("summary", "").strip().lower()
            if key and key in existing_map:
                existing_map[key]["support_count"] = existing_map[key].get("support_count", 1) + 1
                existing_map[key]["updated_at"] = now
            else:
                remaining.append((oi, c))

        if not remaining:
            return merged

        # --- decide: LLM judge path or all-new fallback -------------------------------
        if _judge_results is not None:
            return _apply_judge_results(merged, remaining, _judge_results, now)

        # --- all-new fallback (LLM unavailable) ----------------------------------------
        # 不做语义去重/合并/冲突检测，所有 candidate 作为新规则追加。
        # max_rules + prune 会自动控制规则库大小，避免无限膨胀。
        next_id = len(merged) + 1
        for _oi, c in remaining:
            c["id"] = next_id
            next_id += 1
            c["updated_at"] = now
            merged.append(c)
        return merged

    # ------------------------------------------------------------------
    # Quality scoring (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def score(rule: dict) -> float:
        """Five-dimension quality score used for auto-pruning.

        Dimensions:
          1. ``support_count_norm``  — capped at 5
          2. ``specificity``          — has body text?
          3. ``recency``              — days since creation (90-day linear decay)
          4. ``authority``            — derived from rule ``confidence`` field
          5. ``criticality``          — default 0.7 (``comment``-level)

        Returns a float in [0.0, 1.0].
        """
        now = datetime.now(timezone.utc)

        # 1. Support count (capped at 5)
        support = rule.get("support_count", 1)
        support_norm = min(support, 5) / 5.0

        # 2. Specificity: body text → 1.0, only summary → 0.3
        body = rule.get("body", "") or ""
        specificity = 1.0 if len(body.strip()) > 20 else 0.3

        # 3. Recency: linear decay over 90 days
        created_str = rule.get("created_at", "")
        days = 999.0
        if created_str:
            try:
                created = datetime.fromisoformat(created_str)
                days = (now - created).total_seconds() / 86400.0
            except (ValueError, TypeError):
                pass
        recency = max(0.0, 1.0 - days / 90.0)

        # 4. Authority derived from confidence field
        conf_levels = {"high": 0.9, "medium": 0.7, "low": 0.5}
        authority = conf_levels.get(rule.get("confidence", "medium"), 0.7)

        # 5. Criticality derived from confidence field
        crit_levels = {"high": 0.9, "medium": 0.7, "low": 0.5}
        criticality = crit_levels.get(rule.get("confidence", "medium"), 0.7)

        return (
            _W_SUPPORT * support_norm
            + _W_SPECIFICITY * specificity
            + _W_RECENCY * recency
            + _W_AUTHORITY * authority
            + _W_CRITICALITY * criticality
        )

    @staticmethod
    def prune(rules: list[dict], max_rules: int) -> list[dict]:
        """Drop lowest-scoring rules when ``len(rules) > max_rules``.

        Returns a trimmed list (never exceeds ``max_rules``).
        """
        if max_rules <= 0 or len(rules) <= max_rules:
            return list(rules)

        scored = [(RuleEngine.score(r), r) for r in rules]
        # Sort descending by score, keep top max_rules
        scored.sort(key=lambda x: x[0], reverse=True)
        kept = [r for _, r in scored[:max_rules]]

        dropped = len(rules) - len(kept)
        if dropped > 0:
            logger.info("Pruned %d low-quality rule(s), kept %d", dropped, len(kept))

        return kept

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply(
        self,
        agent_reply: str,
        workflow_rules_path: str,
        max_rules: int = 20,
        min_confidence: str | None = None,
        source: str = "",
    ) -> int:
        """Full pipeline: extract → dedup+merge → score → prune → filter → persist.

        Returns the number of new candidate rules extracted (before
        dedup), or 0 if no rules section was found or the file is
        user-managed.

        *source* is an optional provenance string (e.g. ``"PR #42"``)
        attached to every extracted rule.
        """
        candidates = self.extract(agent_reply)
        if not candidates:
            return 0

        # Fill source provenance
        if source:
            for c in candidates:
                c["source"] = source

        if RuleStore.is_user_managed(workflow_rules_path):
            logger.warning(
                "Rules file %s is user-managed (no auto-managed header); skipping write-back",
                workflow_rules_path,
            )
            return 0

        RuleStore.ensure_file(workflow_rules_path)
        existing_data = RuleStore.load(workflow_rules_path)
        existing = existing_data.get("rules", [])

        merged = self._deduplicate_and_merge(
            candidates,
            existing,
            _judge_results=None,  # all-new fallback
        )

        # Assign/refresh sequential ids after dedup+merge
        for i, r in enumerate(merged, start=1):
            r["id"] = i

        # Backfill conflict references from index-based to ID-based
        for r in merged:
            conflict_idx = r.pop("_conflict_with_idx", None)
            if conflict_idx is not None:
                if isinstance(conflict_idx, list):
                    r["conflict_with"] = [merged[idx]["id"] for idx in conflict_idx]
                else:
                    r["conflict_with"] = [merged[conflict_idx]["id"]]

        merged = self.prune(merged, max_rules=max_rules)

        # Filter by min_confidence
        if min_confidence:
            conf_levels = {"low": 0, "medium": 1, "high": 2}
            min_level = conf_levels.get(min_confidence, 0)
            before = len(merged)
            merged = [
                r for r in merged if conf_levels.get(r.get("confidence", "low"), 0) >= min_level
            ]
            if len(merged) < before:
                logger.info(
                    "Filtered %d rule(s) below min_confidence=%s",
                    before - len(merged),
                    min_confidence,
                )

        RuleStore.save(
            workflow_rules_path,
            merged,
            version=existing_data.get("version", 1),
        )
        logger.info(
            "Extracted %d rule(s), merged to %d total in %s",
            len(candidates),
            len(merged),
            workflow_rules_path,
        )
        return len(candidates)

    def get_rules_path(config: Any, workflow_path: str | None) -> str | None:
        rules_config = getattr(config, "rules", None)
        if not rules_config or not getattr(rules_config, "enabled", False):
            return None
        rules_path = getattr(rules_config, "path", "") or ""
        if not workflow_path:
            return None
        return RuleStore.resolve_path(workflow_path, rules_path)


# ---------------------------------------------------------------------------
# LLM judge result applier
# ---------------------------------------------------------------------------


def _apply_judge_results(
    merged: list[dict],
    remaining: list[tuple[int, dict]],
    judge_results: list[JudgeResult],
    now: str,
) -> list[dict]:
    """Apply ``JudgeResult`` list from LLM to produce the final merged list.

    Each element in *judge_results* corresponds to the **original**
    candidate index (``oi``).  The method processes *remaining*
    candidates that survived exact dedup.
    """
    next_id = len(merged) + 1
    for oi, c in remaining:
        jr = judge_results[oi] if oi < len(judge_results) else None
        if jr is None or jr.action == "new":
            # New rule — append
            c["id"] = next_id
            next_id += 1
            c["updated_at"] = now
            merged.append(c)
        elif jr.action == "duplicate" and jr.target_idx is not None:
            # Duplicate — increment support_count on the target
            target_idx = jr.target_idx
            if target_idx < len(merged):
                merged[target_idx]["support_count"] = merged[target_idx].get("support_count", 1) + 1
                merged[target_idx]["updated_at"] = now
            else:
                # Fallback: target out of range, treat as new
                c["id"] = next_id
                next_id += 1
                c["updated_at"] = now
                merged.append(c)
        elif jr.action == "merge" and jr.target_idx is not None:
            # Merge — combine candidate into the target existing rule
            target_idx = jr.target_idx
            if target_idx < len(merged):
                merged[target_idx] = _merge_two_rules(merged[target_idx], c)
            else:
                c["id"] = next_id
                next_id += 1
                c["updated_at"] = now
                merged.append(c)
        elif jr.action == "conflict" and jr.target_idx is not None:
            # Conflict — keep separate, mark with index refs
            target_idx = jr.target_idx
            if target_idx < len(merged):
                c["_conflict_with_idx"] = target_idx
                merged[target_idx].setdefault("_conflict_with_idx", []).append(len(merged))
                c["updated_at"] = now
                merged.append(c)
            else:
                c["id"] = next_id
                next_id += 1
                c["updated_at"] = now
                merged.append(c)
        else:
            c["id"] = next_id
            next_id += 1
            c["updated_at"] = now
            merged.append(c)
    return merged


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------


def _merge_two_rules(a: dict, b: dict) -> dict:
    """Merge two similar rules into a single enriched rule.

    ``a`` is the existing (higher-confidence) rule; ``b`` is the
    candidate.  The merge picks the best of each field.
    """
    # Summary: pick the longer / more specific one
    summary_a = (a.get("summary") or "").strip()
    summary_b = (b.get("summary") or "").strip()
    merged_summary = summary_a if len(summary_a) >= len(summary_b) else summary_b

    # Body: pick the longer one, or concatenate if both present
    body_a = (a.get("body") or "").strip()
    body_b = (b.get("body") or "").strip()
    if body_a and body_b:
        merged_body = body_a if len(body_a) >= len(body_b) else body_b
    else:
        merged_body = body_a or body_b

    # Category: prefer the more specific one (longer string), or 'multi' if incompatible
    cat_a = (a.get("category") or "").strip()
    cat_b = (b.get("category") or "").strip()
    if cat_a and cat_b and cat_a != cat_b:
        merged_category = "multi"
    else:
        merged_category = cat_a or cat_b or "other"

    # Support count: sum
    support = (a.get("support_count") or 1) + (b.get("support_count") or 1)

    # Source: append if different
    source_a = (a.get("source") or "").strip()
    source_b = (b.get("source") or "").strip()
    merged_source = source_a
    if source_b and source_b not in merged_source:
        merged_source = f"{merged_source}; {source_b}" if merged_source else source_b

    # Confidence: take the higher one
    conf_order = {"low": 0, "medium": 1, "high": 2}
    conf_a = conf_order.get(a.get("confidence", "medium"), 1)
    conf_b = conf_order.get(b.get("confidence", "medium"), 1)
    merged_confidence = (
        "high" if max(conf_a, conf_b) >= 2 else "medium" if max(conf_a, conf_b) >= 1 else "low"
    )

    # Timestamps
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    created_a = a.get("created_at", "") or now
    created_b = b.get("created_at", "") or now
    merged_created = min(created_a, created_b)

    return {
        "summary": merged_summary,
        "body": merged_body,
        "category": merged_category,
        "support_count": support,
        "source": merged_source,
        "confidence": merged_confidence,
        "conflict_with": list(set(a.get("conflict_with", []) + b.get("conflict_with", []))),
        "created_at": merged_created,
        "updated_at": now,
        "last_applied": now,
    }
