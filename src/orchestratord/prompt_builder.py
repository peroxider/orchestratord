"""Build agent prompts from Linear issue data.

Port of Symphony's PromptBuilder (Solid template → Jinja2).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, StrictUndefined, TemplateError

from .agent.task import AgentTask
from .kernel.prompt_core import GENERIC_DEFAULT_PROMPT, get_prompt_router
from .rules_learner import RuleEngine
from .workflow_store import get_workflow_store
from .paths import SESSIONS_DIR

if TYPE_CHECKING:
    from ..capabilities.context_protocol import ContextBuilderProtocol

logger = logging.getLogger(__name__)

# Jinja2 environment with strict undefined handling (mirrors Solid's strict_variables)
_jinja_env = Environment(undefined=StrictUndefined)

# 业务模板（issue/澄清/检视跟进/rebase/premise 注入）已迁至
# business_prompts.py 并经 kernel PromptRouter 注册（DESIGN §4.5/P2）。
# 本模块保留为组装器：store 模板 → 业务 profile → generic 兜底，
# 对业务模块零 import。


class PromptBuilder:
    """Render agent prompts from issue data + workflow config."""

    @staticmethod
    def render(
        task: AgentTask | Any,
        attempt: int | None = None,
        clarification_context: str | None = None,
        pending_question: str | None = None,
        options: list[str] | None = None,
        session: Any | None = None,
        python_executable: str | None = None,
        previous_run_ids: list[str] | None = None,
        previous_verification_error: str | None = None,
        conflict_files: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        """Build prompt using workflow's WORKFLOW.md body template + task data.

        Args:
            task: AgentTask or legacy Issue object with to_dict() method
            attempt: Current attempt number (for retry tracking)
            clarification_context: Pre-rendered clarification guidance block
            pending_question: If issue is in clarification flow, the pending question
            options: If in clarification flow, the available options for the question
            previous_run_ids: Run IDs from previous failed attempts; injected as a
                hint so the agent can Read() past transcripts to learn what was tried.
            conflict_files: when the agent is in a rebase-resolution
                reentry run, this lists the files that git left in conflict
                state. Injected into the prompt so the agent can read each
                file's conflict markers and resolve them.
        """
        store = get_workflow_store()
        current = store.current()

        if current:
            template_str = current[1]
        else:
            # store 未加载：业务 profile（按 task.kind 注册）优先，
            # 未注册回退机制侧 generic 模板。
            template_str = (
                get_prompt_router().profile_template(_input_task_kind(task))
                or GENERIC_DEFAULT_PROMPT
            )

        if not template_str or not template_str.strip():
            template_str = GENERIC_DEFAULT_PROMPT

        try:
            template = _jinja_env.from_string(template_str)
        except TemplateError as exc:
            logger.error("Template parse error: %s", exc)
            template = _jinja_env.from_string(GENERIC_DEFAULT_PROMPT)

        # Detect input type: AgentTask or legacy Issue
        if isinstance(task, AgentTask):
            task_dict = task.to_template_dict()
        elif hasattr(task, "to_dict"):
            # Keep the legacy ``issue`` namespace intact, but expose a
            # task-shaped value as well.  This matters for templates which
            # have already moved to ``task.title`` while callers still pass
            # an Issue to the compatibility API.
            issue_dict = task.to_dict()
            task_dict = {
                "id": issue_dict.get("id", ""),
                "kind": "issue",
                "title": issue_dict.get("title", ""),
                "description": issue_dict.get("description", ""),
                "labels": issue_dict.get("labels", []),
                "priority": issue_dict.get("priority"),
                "attempt": attempt,
                "context": issue_dict,
            }
        else:
            task_dict = dict(task)

        raw_issue_context = task_dict.get("context", task_dict)
        issue_context = (
            dict(raw_issue_context)
            if isinstance(raw_issue_context, dict)
            else dict(task_dict)
        )
        if isinstance(task, AgentTask):
            # ``issue`` is the compatibility namespace used by existing
            # WORKFLOW.md templates.  AgentTask stores tracker fields under
            # explicit ``issue_*`` keys, so restore the legacy aliases rather
            # than handing Jinja a dict that has no ``identifier``/``title``.
            legacy_aliases = {
                "id": issue_context.get("issue_id", task_dict.get("id", "")),
                "identifier": issue_context.get(
                    "issue_identifier",
                    task_dict.get("id", ""),
                ),
                "title": task_dict.get("title", ""),
                "description": task_dict.get("description", ""),
                "labels": task_dict.get("labels", []),
                "priority": task_dict.get("priority"),
            }
        else:
            # Non-AgentTask callers may pass a bare issue dict that lacks
            # "identifier"/"title" while templates render
            # {{ issue.identifier }} / {{ issue.title }} — Jinja2 fails on
            # the missing key ("dict object has no attribute '...'");
            # fill them in so the render never errors.
            legacy_aliases = {
                "identifier": issue_context.get("id", ""),
                "title": "",
                "description": "",
                "labels": [],
            }
        for key, value in legacy_aliases.items():
            issue_context.setdefault(key, value)

        context = {
            "attempt": attempt,
            "task": _to_jinja_value(task_dict),
            # Backward compat: keep "issue" key for existing templates
            "issue": _to_jinja_value(issue_context),
            "clarification": clarification_context,
            "pending_question": pending_question,
            "options": options,
        }

        try:
            rendered = template.render(context).strip()
        except TemplateError as exc:
            logger.error("Template render error: %s", exc)
            # Fallback to default prompt. The fallback itself must
            # never raise — degrade to a minimal raw prompt if even the
            # default cannot render this task shape.
            try:
                fallback = _jinja_env.from_string(GENERIC_DEFAULT_PROMPT)
                rendered = fallback.render(context).strip()
            except TemplateError as fallback_exc:
                logger.error("Default prompt fallback failed: %s", fallback_exc)
                task_value = context.get("task") or {}
                rendered = (
                    f"Task: {task_value.get('title', '')}\n\n"
                    f"{task_value.get('description', '')}"
                ).strip()
        if session is not None and getattr(session, "workspace_strategy", None) == "sequential":
            rendered = f"{rendered}\n\n{_build_sequential_workspace_context(session)}"

        ws_path = _resolve_workspace_path(session)

        # A replacement conversation run has a new request, not a new attempt
        # at the original task. Keep it in the user part, after old context.
        operator_hints = _get_operator_hints(ws_path) if ws_path else None
        followup_request = operator_hints if getattr(session, "run_kind", None) == "agent_followup" else None
        if operator_hints and not followup_request:
            rendered = f"---\n## Operator Hints\n\n{operator_hints}\n---\n\n{rendered}"

        # Root-cause fix: inject workspace diff context so the
        # agent sees exactly which files are already modified and can
        # skip re-exploration when code already exists on disk.
        # Only injected when there are uncommitted changes (first turn).
        ws_diff = _get_workspace_diff(ws_path) if ws_path else None
        if ws_diff:
            rendered = (
                "---\n"
                "## Current Workspace Changes\n"
                "\n"
                "The following files have already been modified or created in the\n"
                "workspace but are not yet committed. If these changes match the\n"
                "current issue's requirements, **do not re-implement them**.\n"
                "Skip directly to `git add` + `git commit`.\n"
                "\n"
                f"{ws_diff}\n"
                "---\n"
                "\n"
                f"{rendered}"
            )

        # Post-render business decorations (premise warning block etc.),
        # registered by business modules into the kernel PromptRouter
        # (DESIGN §4.5). Hooks never raise and are no-ops when no
        # business module has registered (e.g. hermetic mechanism tests).
        for hook in get_prompt_router().post_render_hooks:
            rendered = hook(rendered, task_dict, ws_path)

        if previous_run_ids:
            sessions_home = SESSIONS_DIR
            prev_lines = "\n".join(
                f'- `{rid}` — `Read(path="{sessions_home / rid / "transcript.jsonl"}")`'
                for rid in previous_run_ids
            )
            rendered = (
                "---\n"
                "## Previous Attempts\n"
                "\n"
                "This issue has been attempted before and failed.  You can inspect\n"
                "the full conversation transcript of each previous run to understand\n"
                "what was tried, what went wrong, and what to avoid this time.\n"
                "\n"
                f"{prev_lines}\n"
                "---\n"
                "\n"
                f"{rendered}"
            )

        if previous_verification_error:
            rendered = (
                "---\n"
                "## 上次运行的验证失败（必须处理）\n"
                "\n"
                "上次提交前的检查（pre-commit / 验证命令）失败，输出如下——\n"
                "请先针对失败原因修正代码，确保检查通过后再提交：\n"
                "\n"
                "```\n"
                f"{previous_verification_error[:1500]}\n"
                "```\n"
                "---\n"
                "\n"
                f"{rendered}"
            )

        if python_executable:
            rendered = (
                f"⛔ **约束提醒**：始终用 `{python_executable}` 绝对路径运行 Python，"
                f"不要调试环境差异。\n\n{rendered}"
            )

        # Inject the list of files git left in conflict state so the
        # agent can read each file's conflict markers and resolve them in
        # place. Only emitted when conflict_files is non-empty.
        if conflict_files:
            file_lines = "\n".join(f"- `{name}`" for name in conflict_files)
            rendered = (
                "---\n"
                "## Conflicting Files (rebase reentry)\n"
                "\n"
                "The orchestrator's automated rebase left the following files in a\n"
                "conflict state (REBASE_HEAD is set in the workspace). Read each\n"
                "file, resolve the conflict markers (`<<<<<<<`, `=======`,\n"
                "`>>>>>>>`), then continue the rebase and push:\n"
                "\n"
                f"{file_lines}\n"
                "\n"
                "Suggested commands (run from the workspace root):\n"
                "\n"
                "```bash\n"
                "git status              # confirm REBASE_HEAD state\n"
                "# Edit each file above to remove conflict markers.\n"
                "git add <resolved files>\n"
                "git rebase --continue\n"
                "git push --force-with-lease=origin/<branch>:<remote_sha> \\\n"
                "    origin <branch>\n"
                "```\n"
                "---"
                "\n\n"
                f"{rendered}"
            )

        # Rules file reference injection
        rendered = PromptBuilder._inject_rules_reference_from_store(rendered)

        # inject Task V2 / Logical Kanban guidance so orchestrator-launched
        # agents use the same task-loop discipline as interactive sessions.
        from orchestratord.task_guidelines import get_task_guidelines
        task_guidance = get_task_guidelines()
        if task_guidance:
            rendered = f"{rendered}\n\n---\n{task_guidance}\n---"

        if followup_request:
            system, marker, context = rendered.partition(PromptBuilder.USER_MESSAGE_MARKER)
            if not marker:
                context, system = system, ""
            current = (
                "## Previous task context\n\n"
                "The following describes the earlier task, not a request to repeat it. "
                "Keep its workspace and safety constraints, and use it as context for "
                "the current operator request below.\n\n"
                f"{context.strip()}\n\n"
                "## Current operator request\n\n"
                "Answer or perform this follow-up only. Do not repeat the original "
                "task's actions unless this request asks for them.\n\n"
                f"{followup_request}"
            )
            rendered = f"{system}{marker}\n{current}" if marker else current

        return rendered

    # Prompt split: marker that separates the constant workflow
    # background (system prompt candidate) from the per-issue data
    # (user message candidate) in workflow.md. Lives in workflow.md
    # between the system section and the issue section. The marker is
    # an HTML comment so it is invisible in Markdown rendering.
    USER_MESSAGE_MARKER = "<!-- === USER MESSAGE === -->"

    # ------------------------------------------------------------------
    # Rules reference injection (shared by all prompt flows)
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_rules_reference(prompt: str, rules_path: str | None) -> str:
        """Append a rules file reference line to *prompt* if *rules_path* is set.

        The reference is deliberately a single line — the agent is expected
        to ``Read()`` the file on demand ("参考示例而非强制约束").
        """
        if not rules_path:
            return prompt
        return (
            f"{prompt}\n\n"
            f"---\n"
            f"\U0001f4d0 **Review conventions**: `{rules_path}`\n"
            f"The file contains illustrative conventions extracted from "
            f"previous PR reviews. Read it with `Read()` when relevant \u2014 "
            f"the rules are **reference examples**, not mandatory requirements.\n"
            f"---"
        )

    @staticmethod
    def render_parts(
        issue: Any,
        attempt: int | None = None,
        clarification_context: str | None = None,
        pending_question: str | None = None,
        options: list[str] | None = None,
        session: Any | None = None,
        python_executable: str | None = None,
        previous_run_ids: list[str] | None = None,
        previous_verification_error: str | None = None,
        conflict_files: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[str, str]:
        """Render prompt split into (system, user) by USER_MESSAGE_MARKER.

        The marker lives in workflow.md between the constant background
        / constraint block (system) and the per-issue data block (user).
        The system part is appended to the headless session's effective
        system prompt (alongside CLAUDE.md + git status + style) so the
        daemon sees the same "rich system + short user" structure as
        CCB's interactive session. The user part becomes the per-turn
        user message.

        Falls back to ("", full) when the marker is missing so callers
        that pass an old / un-migrated workflow.md still work — the full
        prompt lands in user and the system append is empty.

        ``@agent-<type>`` mentions in either half of the prompt are
        passed through verbatim to the agent. The orchestrator does not
        perform ``@agent-`` expansion; that capability was removed when
        the multi-agent expansion layer was deleted. Unknown agents are
        not stripped: the literal text remains in the user prompt so
        the agent can interpret it (or ignore it) at runtime.
        """
        full = PromptBuilder.render(
            issue,
            attempt=attempt,
            clarification_context=clarification_context,
            pending_question=pending_question,
            options=options,
            session=session,
            python_executable=python_executable,
            previous_run_ids=previous_run_ids,
            previous_verification_error=previous_verification_error,
            conflict_files=conflict_files,
        )
        marker = PromptBuilder.USER_MESSAGE_MARKER
        if marker in full:
            system_part, user_part = full.split(marker, 1)
            return system_part.strip(), user_part.strip()
        return "", full.strip()

    @staticmethod
    def _inject_rules_reference_from_store(prompt: str) -> str:
        """Resolve rules path from WorkflowStore and inject reference."""
        store = get_workflow_store()
        current = store.current()
        if not current:
            return prompt
        config = current[0]
        workflow_path = getattr(config, "source_path", None) or getattr(
            config, "_source_path", None
        )
        rules_path = RuleEngine.get_rules_path(config, workflow_path)
        return PromptBuilder._inject_rules_reference(prompt, rules_path)

    @staticmethod
    def build_continuation_prompt(
        turn_number: int,
        max_turns: int,
        issue_context: str | None = None,
        session: Any | None = None,
        python_executable: str | None = None,
    ) -> str:
        """Build continuation prompt for subsequent turns.

        Root-cause fix: inject a summary of recent git commits
        so the LLM can see what has already been done in previous
        turns and avoid re-exploring from scratch.
        """
        context_block = f"\n\nCurrent issue context:\n{issue_context}\n" if issue_context else ""
        urgency = (
            f"\n- ⚠️  You have only {max_turns - turn_number + 1} turn(s) remaining. "
            f"Prioritize code implementation over reading more files. "
            f"Use Write/Edit to make concrete changes NOW."
            if turn_number >= max_turns // 2
            else ""
        )

        # Root-cause fix: inject recent git log so the LLM knows
        # what was already done in previous turns.
        git_log_summary = _get_git_log_summary(session)

        # Operator hints injection for continuation turns.
        ws_path = _resolve_workspace_path(session)
        operator_hints = _get_operator_hints(ws_path) if ws_path else None
        hints_block = (
            f"---\n## Operator Hints\n\n{operator_hints}\n---\n\n" if operator_hints else ""
        )

        python_constraint = (
            f"⛔ **约束提醒**：始终用 `{python_executable}` 绝对路径，不要调试环境差异。\n"
            if python_executable
            else ""
        )

        prompt = (
            f"{hints_block}"
            f"Continuation guidance:\n\n"
            f"{python_constraint}"
            f"⛔ `pytest` 禁止使用管道 `| tail -40`/`| head -50`，用 `--tb=short -q` 替代。\n"
            f"⛔ 建议终端命令时用 `orchestratord` CLI，不要用 `python3 -c` 或 `PYTHONPATH=`。\n"
            f"- This is continuation turn #{turn_number} of {max_turns}.{context_block}{urgency}\n"
            f"- Resume from the current workspace state and continue implementing.\n"
            f"- Use available tools (Bash, Write, Edit, Grep, Glob, etc.) to make changes.\n"
            f"- Focus on completing the issue requirements. Do NOT re-read files you have already explored.\n"
            f"- Your FIRST action should be a Write or Edit to implement the feature.\n"
            f"{git_log_summary}"
        )
        # Inject rules reference on continuation turns
        prompt = PromptBuilder._inject_rules_reference_from_store(prompt)
        return prompt


def _input_task_kind(task: Any) -> str:
    """Extract the task kind before the full template dict is built.

    AgentTask carries ``kind`` directly; legacy Issue objects are
    inherently issue-shaped; bare dicts carry ``kind`` when present.
    """
    if isinstance(task, AgentTask):
        return task.kind
    if hasattr(task, "to_dict"):
        return "issue"
    if isinstance(task, dict):
        return str(task.get("kind", ""))
    return ""


def _build_sequential_workspace_context(session: Any) -> str:
    return "\n".join(
        [
            "---",
            "## Sequential Workspace Context",
            "",
            "This issue is running in a sequential shared workspace.",
            f"- Workspace strategy: `{getattr(session, 'workspace_strategy', 'sequential')}`",
            f"- Integration branch: `{getattr(session, 'integration_branch', None) or 'current branch'}`",
            f"- Start commit: `{getattr(session, 'start_commit_sha', None) or 'unknown'}`",
            f"- Base commit: `{getattr(session, 'base_commit_sha', None) or 'unknown'}`",
            f"- Previous issue: `{getattr(session, 'previous_issue_id', None) or 'none'}`",
            f"- Sequence index: `{getattr(session, 'sequence_index', None) or 'unknown'}`",
            "",
            "Build on the existing commit chain in this workspace. Do not redo earlier issues.",
            "If the expected prior commit chain appears to be missing, stop and report it.",
            "---",
        ]
    )


def _to_jinja_value(value: Any) -> Any:
    """Coerce a value into Jinja2-friendly shapes."""
    if isinstance(value, dict):
        return {str(k): _to_jinja_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jinja_value(v) for v in value]
    return value


def _resolve_workspace_path(session: Any) -> Path | None:
    """Extract the workspace root path from a session object.

    Returns None when there is no session or no workspace, which means
    the workspace-diff context is silently skipped.
    """
    if session is None:
        return None
    ws = getattr(session, "workspace", None)
    if ws is None:
        return None
    path = getattr(ws, "path", None)
    if path is None:
        return None
    return Path(path)


def _get_workspace_diff(ws_path: Path) -> str | None:
    """Run ``git diff --stat`` and ``git status --short`` in the
    workspace to produce a compact summary of uncommitted changes.

    Returns ``None`` when the workspace is clean (no changes), so the
    caller can skip injecting the diff context block entirely.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff_stat = proc.stdout.strip()
        proc2 = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        status_short = proc2.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if not diff_stat and not status_short:
        return None  # clean workspace — nothing to inject
    parts = []
    if diff_stat:
        parts.append(f"```\n{diff_stat}\n```")
    if status_short:
        parts.append(f"Uncommitted files:\n```\n{status_short}\n```")
    return "\n".join(parts)


def _get_operator_hints(ws_path: Path) -> str | None:
    """Read ``.operator_hints.md`` from workspace, return contents.

    Inject hints are one-shot: once read they are removed so the agent
    sees them exactly once (the next turn-boundary prompt) and historical
    injects do not accumulate across turns or runs.

    However, ``repro_gate.append_repro_hint`` also writes to this file —
    a ``## Reproduction established`` section that MUST persist across
    every turn's prompt until the fix is complete. This method preserves
    that section when clearing inject hints.

    Returns ``None`` when the file is missing or empty so callers can
    skip injecting the operator-hints block entirely.
    """
    hints_file = ws_path / ".operator_hints.md"
    if not hints_file.exists():
        return None
    try:
        content = hints_file.read_text(encoding="utf-8").strip()
        # Preserve the ## Reproduction established section (from
        # repro_gate.append_repro_hint) which must persist across turns.
        # Only clear inject hints (one-shot semantics).
        repro_idx = content.find("## Reproduction established")
        repro_section = content[repro_idx:] if repro_idx >= 0 else ""
        hints_file.write_text(repro_section, encoding="utf-8")
        if content:
            return content
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read operator hints from %s: %s", hints_file, exc)
    return None


def _get_git_log_summary(session: Any) -> str:
    """Run ``git log --oneline -3`` in the workspace and return a
    compact summary of recent commits, or an empty string when there
    is no session / workspace / git history.

    Root-cause fix: injected into continuation prompts so the
    LLM can see what has already been committed in previous turns
    and avoid re-exploring from scratch.
    """
    if session is None:
        return ""
    ws = getattr(session, "workspace", None)
    if ws is None:
        return ""
    ws_path = getattr(ws, "path", None)
    if ws_path is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        log_out = proc.stdout.strip()
        if not log_out:
            return ""
        return f"\nRecent commits in workspace:\n```\n{log_out}\n```\n"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


# ─── Python interpreter detection + cascade resolver ────────────────
# Workspace-level python_executable: detect the interpreter
# inside the target repo via project-level signals, then expose a
# cascade so the prompt builder can pick the most specific value.


def _parse_pyvenv_home(cfg_path: Path) -> str:
    """Extract ``home = <path>`` from a pyvenv.cfg file.

    Returns the home directory string or ``""`` on parse failure /
    missing file. Soft-fails by design: malformed pyvenv.cfg must
    not block prompt rendering.
    """
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("home"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    return ""


def _parse_conda_env_name(yml_path: Path) -> str:
    """Extract the conda env ``name:`` from an environment.yml.

    Returns the env name or ``""`` if no ``name:`` key is set.
    """
    try:
        text = yml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.lower().startswith("name"):
            _, _, value = stripped.partition(":")
            return value.strip().strip('"').strip("'")
    return ""


# Ordered conda root locations probed when an ``environment.yml`` is
# present. ``CONDA_PREFIX`` is consulted first when set (covers any
# non-standard install location), then the common defaults.
_CONDA_ROOT_CANDIDATES: tuple[str, ...] = (
    "/opt/conda",
    "/root/anaconda3",
    "/root/miniconda3",
    "/usr/local/anaconda3",
    "/usr/local/miniconda3",
    "/opt/anaconda3",
)


def _detect_python_in_workspace(
    workspace_path: Path | None,
    candidates: list[str],
) -> str:
    """Walk a list of project-level signals and return the absolute
    path of the first Python interpreter that can be derived from
    them. Returns ``""`` when nothing matches.

    Soft-fails: missing files, malformed contents, or non-existent
    interpreter binaries are silently skipped — the function is
    best-effort and never raises.

    Recognised probe kinds (matched by relative path):

    * ``.python-version`` — pyenv version spec; resolved against
      ``$PYENV_ROOT`` or ``~/.pyenv/versions/<v>/bin/python3``.
    * ``pyvenv.cfg`` and ``.venv/pyvenv.cfg`` — venv / uv / poetry
      venv markers; the ``home = ...`` line gives the venv root.
    * ``environment.yml`` — conda env file; ``name:`` is matched
      against ``$CONDA_PREFIX`` and a set of well-known conda
      install prefixes.
    * ``Pipfile`` and ``pyproject.toml`` — recognised but skipped
      because they describe dependencies rather than interpreter
      paths. Listed in the default candidates so operators can
      disable them via ``python_detect_files`` if desired.
    """
    if workspace_path is None:
        return ""
    workspace_path = Path(workspace_path)
    if not workspace_path.exists():
        return ""

    for rel in candidates:
        f = workspace_path / rel
        if not f.exists() or not f.is_file():
            continue
        try:
            if rel == ".python-version":
                version = f.read_text(encoding="utf-8", errors="replace").strip()
                if version:
                    pyenv_root = Path(os.environ.get("PYENV_ROOT", str(Path.home() / ".pyenv")))
                    py = pyenv_root / "versions" / version / "bin" / "python3"
                    if py.exists():
                        return str(py)
            elif rel.endswith("pyvenv.cfg"):
                home = _parse_pyvenv_home(f)
                if home:
                    py = Path(home) / "bin" / "python3"
                    if py.exists():
                        return str(py)
            elif rel == "environment.yml":
                env_name = _parse_conda_env_name(f)
                if env_name:
                    conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
                    roots: list[str] = [conda_prefix] if conda_prefix else []
                    roots += list(_CONDA_ROOT_CANDIDATES)
                    for root in roots:
                        if not root:
                            continue
                        py = Path(root) / "envs" / env_name / "bin" / "python3"
                        if py.exists():
                            return str(py)
            elif rel in ("Pipfile", "pyproject.toml"):
                continue
        except OSError:
            continue
    return ""


def resolve_python_executable(
    *,
    workspace_path: Path | None,
    agent_cfg: Any,
    workspace_cfg: Any,
    issue_executable: str = "",
) -> str:
    """Cascade resolver: pick the most specific Python interpreter
    path available.

    Resolution order (first non-empty wins):

    1. ``issue_executable`` — per-issue override (e.g. from
       ``LocalTracker`` frontmatter ``python_executable: ...``).
       Highest priority because a single issue may legitimately
       need a different interpreter than its sibling issues in the
       same workspace.
    2. ``workspace_cfg.python_executable`` — explicit per-workspace
       override (handles "different repo needs different python").
    3. Auto-detected path via
       :func:`_detect_python_in_workspace` when
       ``workspace_cfg.python_auto_detect`` is True.
    4. ``agent_cfg.python_executable`` — workflow-wide default
       (the MVP-1 knob).
    5. Empty string — caller should treat as "no constraint"; the
       agent will rely on PATH ``python3``.

    Args:
        workspace_path: Absolute path to the workspace directory
            (``Workspace.path``), or ``None`` when there is no
            workspace yet (e.g. unit tests).
        agent_cfg: An ``AgentConfig``-like object exposing
            ``python_executable``.
        workspace_cfg: A ``WorkspaceConfig``-like object exposing
            ``python_executable``, ``python_auto_detect`` and
            ``python_detect_files``.
        issue_executable: Per-issue override string. ``""`` (the
            default) skips this level entirely. Provided by the
            caller from ``Issue.python_executable`` (populated by
            ``LocalTrackerAdapter`` from the issue markdown
            frontmatter).

    Returns:
        Absolute path string, or ``""`` when no constraint applies.
    """
    issue_override = (issue_executable or "").strip()
    if issue_override:
        return issue_override

    ws_explicit = getattr(workspace_cfg, "python_executable", "") or ""
    if ws_explicit:
        return ws_explicit

    auto_detect = getattr(workspace_cfg, "python_auto_detect", True)
    if auto_detect:
        detect_files = list(
            getattr(workspace_cfg, "python_detect_files", None)
            or [
                ".python-version",
                "pyvenv.cfg",
                ".venv/pyvenv.cfg",
                "Pipfile",
                "environment.yml",
            ]
        )
        detected = _detect_python_in_workspace(workspace_path, detect_files)
        if detected:
            return detected

    agent_default = getattr(agent_cfg, "python_executable", "") or ""
    if agent_default:
        return agent_default

    return ""
