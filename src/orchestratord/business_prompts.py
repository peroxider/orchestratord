"""issue→PR 业务的 prompt 模板与渲染器（DESIGN §4.5 / P2）。

P4 之前作为临时业务模块存在（DESIGN §7 P2）；P4 将并入
``applications/issue_pr/prompts.py`` 并经 ``IssueToPrApplication
.prompt_profiles()`` 注册。本模块在 import 时把业务模板注册进
kernel 的 :class:`~orchestratord.kernel.prompt_core.PromptRouter`
（幂等），组合根（orchestration_subsystem / applications 注册表）
import 本模块即完成装配。

迁移自 prompt_builder.py：``_CLARIFICATION_TEMPLATE``、
``_REVIEW_FEEDBACK_TEMPLATE``、``render_rebase``、
``render_review_feedback``、``render_feedback_summary``、
``build_clarification_context`` 与 premise 警告块注入逻辑。
文本逐字节保留——P2 验收要求渲染结果 byte-identical。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import TemplateError

from orchestratord.kernel.prompt_core import (
    GENERIC_DEFAULT_PROMPT,
    _jinja_env,
    get_prompt_router,
)

from .premise_check import build_premise_block, check_issue_premise
from .prompt_builder import PromptBuilder, _to_jinja_value
from .tracker import PullRequestFeedback, PullRequestRef

logger = logging.getLogger(__name__)

# Jinja2 template for clarification guidance injected into the prompt.
# Rendered when an issue is in the clarification flow.
_CLARIFICATION_TEMPLATE = """
---
## Clarification Context

{% if clarification_answer %}
The issue author or operator supplied clarification before this run. Treat the
answer below as part of the issue requirements.

- Question: "{{ pending_question or 'Pre-dispatch clarification' }}"
- Answer{% if answer_source %} ({{ answer_source }}){% endif %}: "{{ clarification_answer }}"
{% else %}
This issue is currently awaiting clarification. When the answer is available,
it will be provided below. If you are unsure about any aspect of the issue,
use the `AskIssueAuthor` tool to request clarification from the issue author
or local operator.

When requesting clarification:
- Be specific: ask exactly what is ambiguous (e.g., "Should this function be sync or async?")
- Provide context: include relevant code snippets or error messages
- Limit to one question at a time to avoid overwhelming responders
{% if pending_question %}
- Current pending question: "{{ pending_question }}"
{% if options %}
- Available options: {{ options|join(', ') }}
{% endif %}
{% endif %}
{% endif %}
---"""

_REVIEW_FEEDBACK_TEMPLATE = """You are an autonomous software engineering agent fixing pull request feedback.

Issue: {{ issue.identifier }} - {{ issue.title }}
Pull request: {% if pull_request.number %}#{{ pull_request.number }}{% else %}unknown{% endif %}{% if pull_request.url %} ({{ pull_request.url }}){% endif %}
Branch: {{ branch_name }}

Current task:
- Fix only the PR review feedback and CI failures listed below.
- Do not expand scope or reimplement unrelated issue requirements.
- Work on the current branch only; do not create a new branch or pull request.
- Prefer the smallest correct change that addresses the feedback.
- If feedback is conflicting or unclear, leave code unchanged for that item and explain what clarification is needed.
- Run relevant tests or record why they cannot be run.
- CLI Usage: when suggesting terminal commands, use `orchestratord` not `python3 -c` or `PYTHONPATH=`.

Feedback:
{% for item in feedback %}
{{ loop.index }}. [{{ item.source }}] {{ item.id }}{% if item.severity %} severity={{ item.severity }}{% endif %}{% if item.status %} status={{ item.status }}{% endif %}
{% if item.file_path %}   File: {{ item.file_path }}{% if item.line %}:{{ item.line }}{% endif %}
{% endif %}{% if item.commit_sha %}   Commit: {{ item.commit_sha }}
{% endif %}{% if item.url %}   URL: {{ item.url }}
{% endif %}{% if item.diff_hunk %}   Diff hunk:
```diff
{{ item.diff_hunk }}
```
{% endif %}   Body:
{{ item.body | indent(3) }}
{% endfor %}
"""

#: issue 任务的业务模板（当前与机制侧 generic 兜底逐字节一致；
#: 注册进 PromptRouter 后即成为业务可独立演进的接缝，P4 迁移
#: ``applications/issue_pr/prompts.py``）。
ISSUE_PROMPT_TEMPLATE = GENERIC_DEFAULT_PROMPT


def render_rebase(
    *,
    issue: Any,
    branch_name: str,
    base_branch: str,
    conflict_files: tuple[str, ...] | list[str] = (),
    reason: str | None = None,
) -> str:
    """Build a prompt for an agent run that resolves a rebase conflict.

    This is used when ``_process_rebase_intent`` left content conflicts
    (has_conflict=True) and the daemon launches a fresh ``agent_rebase``
    run to resolve them. The prompt is intentionally minimal — the agent
    is told exactly which files git marked as conflicting and the
    suggested git commands to finish the rebase + push.
    """
    issue_dict = issue.to_dict() if hasattr(issue, "to_dict") else issue
    title = (
        issue_dict.get("title") if isinstance(issue_dict, dict) else getattr(issue, "title", "")
    ) or ""
    identifier = (
        issue_dict.get("identifier")
        if isinstance(issue_dict, dict)
        else getattr(issue, "identifier", "")
    ) or ""

    files_block = (
        "\n".join(f"- `{name}`" for name in conflict_files)
        if conflict_files
        else "- (no specific files reported — run `git diff --name-only --diff-filter=U` to list them)"
    )

    reason_block = f"\n## Reason\n\n{reason}\n" if reason else ""

    template = (
        "---\n"
        f"# PR Conflict Resolution — {identifier}\n"
        f"\n**Title:** {title}\n"
        f"**Branch:** `{branch_name}` (base `{base_branch}`)\n"
        f"{reason_block}"
        "\n"
        "## Task\n"
        "\n"
        "The orchestrator's automated `git rebase origin/<base>` left this\n"
        "branch with content conflicts. Your job is to resolve each conflict,\n"
        "continue the rebase, and push the rebased branch with\n"
        "`--force-with-lease` (the default) so the PR becomes mergeable\n"
        "again. **Do NOT close the PR or open a new one.**\n"
        "\n"
        "## Conflicting Files\n"
        "\n"
        f"{files_block}\n"
        "\n"
        "## Procedure\n"
        "\n"
        "1. `git status` — confirm REBASE_HEAD is set.\n"
        "2. For each file above: read the file, remove the\n"
        "   `<<<<<<<`/`=======`/`>>>>>>>` markers, write the merged\n"
        "   content you want kept.\n"
        "3. `git add <file>` for each resolved file.\n"
        "4. `git rebase --continue` (or `--skip` if the upstream commit is\n"
        "   the one to drop — but only when clearly safe).\n"
        "5. `git log --oneline -5` to verify the rebased history.\n"
        "6. Capture the new `HEAD` SHA, then push:\n"
        "\n"
        "   ```bash\n"
        "   REMOTE_SHA=$(git rev-parse origin/<branch>)\n"
        "   git push --force-with-lease=<branch>:$REMOTE_SHA origin <branch>\n"
        "   ```\n"
        "\n"
        "7. Print the final head SHA in your response so the orchestrator\n"
        "   can record it.\n"
        "\n"
        "## Constraints\n"
        "\n"
        "- **Do not** run `git rebase --abort` unless explicitly asked; we\n"
        "  want the rebased history, not the pre-rebase one.\n"
        "- **Do not** use plain `git push --force`; the orchestrator\n"
        "  defaults to `--force-with-lease` to avoid clobbering concurrent\n"
        "  pushes. Only use `--force` if the operator explicitly passed\n"
        "  `--force` to the rebase CLI.\n"
        "- **Do not** open a new PR; the existing PR will pick up the\n"
        "  rebased head automatically once the push lands.\n"
        "---"
    )
    return template


def render_review_feedback(
    *,
    issue: Any,
    pull_request: PullRequestRef,
    branch_name: str,
    feedback: list[PullRequestFeedback],
) -> str:
    issue_dict = issue.to_dict() if hasattr(issue, "to_dict") else issue
    context = {
        "issue": _to_jinja_value(issue_dict),
        "pull_request": pull_request,
        "branch_name": branch_name,
        "feedback": feedback,
    }
    try:
        rendered = _jinja_env.from_string(_REVIEW_FEEDBACK_TEMPLATE).render(context).strip()
    except TemplateError as exc:
        logger.error("Review feedback template render error: %s", exc)
        return GENERIC_DEFAULT_PROMPT

    # Inject rules reference
    rendered = PromptBuilder._inject_rules_reference_from_store(rendered)
    return rendered


def render_feedback_summary(
    *,
    attempt: int,
    processed: list[PullRequestFeedback],
    skipped: list[dict],
) -> str:
    """Render a post-followup summary for the PR.

    Args:
        attempt: Follow-up attempt number.
        processed: Feedback items that were auto-handled.
        skipped: Dicts with keys ``feedback`` (PullRequestFeedback)
            and ``reason`` (str) for items needing human attention.
    """
    lines = [
        "## Orchestratord PR Review Follow-up Summary",
        "",
        f"**Follow-up attempt**: #{attempt}",
        f"**Processed**: {len(processed)} item(s)",
    ]
    if processed:
        lines += ["", "### Auto-handled"]
        for item in processed:
            loc = ""
            if item.file_path:
                loc = f" (`{item.file_path}"
                if item.line:
                    loc += f":{item.line}"
                loc += "`)"
            body_preview = (item.body or "")[:80]
            if len(item.body or "") > 80:
                body_preview += "..."
            lines.append(f"- [{item.source}] {item.id}{loc}: {body_preview}")
    if skipped:
        lines += ["", "### Needs human attention"]
        for entry in skipped:
            fb = entry["feedback"]
            reason = entry["reason"]
            loc = ""
            if fb.file_path:
                loc = f" (`{fb.file_path}"
                if fb.line:
                    loc += f":{fb.line}"
                loc += "`)"
            lines.append(f"- [{fb.source}] {fb.id}{loc}: {reason}")
    return "\n".join(lines)


def build_clarification_context(
    pending_question: str | None = None,
    options: list[str] | None = None,
    clarification_answer: str | None = None,
    answer_source: str | None = None,
) -> str:
    """Build a clarification guidance block for the system prompt.

    This text is injected into the agent's prompt when an issue is in
    the clarification flow, guiding the agent to use AskIssueAuthor
    correctly and informing it about any pending question.

    Args:
        pending_question: The pending clarification question, if any
        options: Available options (for multiple-choice questions)

    Returns:
        A formatted clarification guidance block, or empty string if
        clarification is not active
    """
    if not pending_question and not clarification_answer:
        return ""

    template_str = _CLARIFICATION_TEMPLATE.strip()
    try:
        template = _jinja_env.from_string(template_str)
    except TemplateError as exc:
        logger.error("Clarification template parse error: %s", exc)
        return ""

    context = {
        "pending_question": pending_question,
        "options": options or [],
        "clarification_answer": clarification_answer,
        "answer_source": answer_source,
    }
    try:
        return template.render(context).strip()
    except TemplateError as exc:
        logger.error("Clarification template render error: %s", exc)
        return ""


def _premise_warning_hook(
    rendered: str, task_dict: dict[str, Any], ws_path: Path | None
) -> str:
    """Premise check (defect R3): when the issue references files that do
    not exist in the workspace, warn the agent up front and hand it the
    honest-exit protocol, so "fabricate the missing file" is no longer
    the path of least resistance.

    迁移自 prompt_builder.render() 内联段；行为逐字节保留（含
    fail-open 语义：premise 检查永不阻断/破坏 prompt 渲染）。
    """
    if ws_path is None:
        return rendered
    try:
        missing_paths = check_issue_premise(task_dict, str(ws_path))
    except Exception:  # premise checking must never break prompts
        logger.debug("premise check failed", exc_info=True)
        missing_paths = []
    if missing_paths:
        return f"{rendered}\n\n{build_premise_block(missing_paths)}"
    return rendered


def register_business_prompts() -> None:
    """把业务模板与装饰钩子注册进 kernel PromptRouter（幂等）。"""
    router = get_prompt_router()
    router.register_profile("issue", ISSUE_PROMPT_TEMPLATE)
    router.register_hook(_premise_warning_hook)


# Import 即注册（组合根与应用注册表 import 本模块即完成装配）。
register_business_prompts()
