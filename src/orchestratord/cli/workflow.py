"""orchestrator workflow — scaffold a workflow.md from the packaged template.

Usage (noun-verb):
  orchestratord workflow init [--kind <tracker>] [--owner <o>] [--repo <r>]

Design:
  - Copies the workflow template from the package to the current directory.
  - Replaces ``{{PLACEHOLDER}}`` tokens with user-provided values.
  - The template path is resolved from the installed package, so users
    who installed via ``pip`` (no source tree) can still generate a
    workflow.md without manually hunting for template files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ── Template resolution ──────────────────────────────────────────────


def _template_path(variant: str = "workflow") -> Path:
    """Return the path to ``{variant}.template.md`` inside the package.

    Works for both editable installs (source tree) and non-editable
    installs (site-packages zip / wheel).

    Raises ``FileNotFoundError`` if the variant does not exist.
    """
    import orchestratord.templates as tpl_mod

    # Try named variant first, fall back to "workflow" generic name.
    # Supports both {variant}.template.md and {variant}.template (e.g. workflow.yaml.template)
    candidates: list[str] = []
    if "." in variant:
        # e.g. "workflow.yaml" → "workflow.yaml.template"
        candidates.append(f"{variant}.template")
    candidates.append(f"{variant}.template.md")
    candidates.append("workflow.template.md")

    for p in tpl_mod.__path__:  # type: ignore[attr-defined]
        for name in candidates:
            candidate = Path(p) / name
            if candidate.exists():
                return candidate

    # Python 3.9+ importlib.resources API as second resort
    try:
        from importlib.resources import files

        for name in candidates:
            ref = files("orchestratord.templates") / name  # type: ignore[arg-type]
            if hasattr(ref, "__fspath__"):
                p = Path(ref.__fspath__())
                if p.exists():
                    return p
            # MultiplexedPath
            if str(ref) and Path(str(ref)).exists():
                return Path(str(ref))
    except Exception:
        pass

    # Last resort: walk sys.modules for the orchestrator package
    import orchestratord as orch_mod

    base = Path(orch_mod.__file__).parent  # type: ignore[arg-type]
    for name in candidates:
        candidate = base / "templates" / name
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"Cannot locate template '{variant}' — your install may be corrupt. "
        f"Run 'orchestratord workflow list-templates' to see available variants."
    )


def _available_templates() -> dict[str, str]:
    """Return {variant_name: template_path} for all packaged templates."""
    import orchestratord.templates as tpl_mod

    templates: dict[str, str] = {}
    for p in tpl_mod.__path__:  # type: ignore[attr-defined]
        for f in Path(p).glob("*.template.md"):
            name = f.name.removesuffix(".template.md")
            templates[name] = str(f)
        for f in Path(p).glob("*.yaml.template"):
            name = f.name.removesuffix(".template")
            templates[name] = str(f)
    return templates


# ── Placeholder substitution ─────────────────────────────────────────


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt interactively, or return *default* when stdin is not a TTY."""
    if not sys.stdin.isatty():
        return default
    try:
        if secret:
            import getpass

            raw = getpass.getpass(f"  {label} [{default}]: ")
        else:
            raw = input(f"  {label} [{default}]: ")
        return raw.strip() or default
    except EOFError:
        # Ctrl+D / closed stdin: fall back to the default and continue.
        # KeyboardInterrupt (Ctrl+C) intentionally propagates so the CLI
        # aborts instead of silently skipping to the next prompt.
        return default


def _fill_placeholders(content: str, values: dict[str, str]) -> str:
    """Replace ``{{KEY}}`` and ``<KEY>`` placeholders with corresponding *values*.

    Handles two placeholder styles:
    - ``{{KEY}}`` — used by the remote-tracker workflow template
    - ``<KEY>``   — used by the local-tracker workflow template and issue cards
    """
    for key, val in values.items():
        content = content.replace("{{" + key + "}}", val)
        # Also replace ${{KEY}}_ENV style
        content = content.replace("${{" + key + "_ENV}}", val)
        # Also replace <KEY> style (used by workflow-local.template.md)
        content = content.replace("<" + key + ">", val)
    return content


# ── Template ↔ tracker-kind rules ────────────────────────────────────
#
# Derived from the tracker registry (``orchestratord.tracker_kinds``) so
# the CLI can never drift from the adapter layer.


def _template_variant_for_kind(kind: str) -> str:
    """Return the canonical workflow template variant for a tracker kind."""
    return "workflow-local" if kind == "local" else "workflow"


def _compatible_kinds(variant: str) -> set[str]:
    """Return tracker kinds whose canonical template is *variant*."""
    from orchestratord.tracker_kinds import SUPPORTED_TRACKERS

    return {k for k in SUPPORTED_TRACKERS if _template_variant_for_kind(k) == variant}


# ── Parser ───────────────────────────────────────────────────────────


def add_workflow_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register workflow definition management commands."""
    parser = subparsers.add_parser(
        "workflow",
        help="Scaffold and manage orchestrator workflow files",
        description="Generate ``workflow.md`` from the packaged template, "
        "or list available template variants.",
    )
    wf_sub = parser.add_subparsers(
        dest="workflow_subcommand",
        required=True,
    )

    # --- workflow init ---
    init_parser = wf_sub.add_parser(
        "init",
        help="Generate workflow.md from template",
        description="Copy the packaged workflow template to the current directory "
        "and replace placeholders with your values. "
        "Use --workflow-yaml to also generate a declarative workflow.yaml "
        "for multi-stage DAG execution with quality gates and decision branches.",
    )
    init_parser.add_argument(
        "--template",
        "-t",
        default="workflow",
        metavar="VARIANT",
        help="Template variant: workflow (default, remote tracker), "
        "workflow-local (local file-based tracker). Interactive init "
        "selects the variant automatically from the tracker kind. "
        "Run 'list-templates' to see all available variants.",
    )
    init_parser.add_argument(
        "--kind",
        "-k",
        default="",
        metavar="TRACKER",
        help="Tracker kind: github, gitcode, gitee, linear, local "
        "(the template variant switches to match)",
    )
    init_parser.add_argument(
        "--owner",
        "-o",
        default="",
        metavar="OWNER",
        help="Upstream repository owner (e.g. my-org)",
    )
    init_parser.add_argument(
        "--repo",
        "-r",
        default="",
        metavar="REPO",
        help="Repository name (e.g. my-project)",
    )
    init_parser.add_argument(
        "--endpoint",
        default="",
        metavar="URL",
        help="API endpoint for self-hosted instances",
    )
    init_parser.add_argument(
        "--assignee",
        default="",
        metavar="USER",
        help="Only process issues assigned to this user",
    )
    init_parser.add_argument(
        "--branch-prefix",
        default="orchestratord",
        metavar="PREFIX",
        help="Branch prefix for issue branches (default: orchestratord)",
    )
    init_parser.add_argument(
        "--workspace-root",
        default="/tmp/orchestratord_workspaces/myproject",
        metavar="PATH",
        help="Local workspace root path",
    )
    init_parser.add_argument(
        "--fork-owner",
        default="",
        metavar="OWNER",
        help="Fork owner for fork workflow (empty or same as owner = single-repo mode)",
    )
    init_parser.add_argument(
        "--output",
        "--out",
        default="workflow.md",
        metavar="FILE",
        help="Output file path (default: ./workflow.md)",
    )
    init_parser.add_argument(
        "--workflow-yaml",
        action="store_true",
        help="Also generate a declarative workflow.yaml for DeclarativeWorkflowEngine",
    )
    init_parser.add_argument(
        "--workflow-name",
        default="",
        metavar="NAME",
        help="Workflow name for workflow.yaml (default: {repo}-code-review)",
    )
    init_parser.add_argument(
        "--workflow-yaml-output",
        default="workflow.yaml",
        metavar="FILE",
        help="Output path for workflow.yaml (default: ./workflow.yaml)",
    )
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip prompts; use defaults for missing values",
    )

    # --- workflow list-templates ---
    wf_sub.add_parser(
        "list-templates",
        help="List available workflow template variants",
        description="Show all packaged template files and their locations.",
    )
    validate_parser = wf_sub.add_parser("validate", help="Validate a declarative workflow")
    validate_parser.add_argument("file", metavar="WORKFLOW_YAML")
    show_parser = wf_sub.add_parser("show", help="Show a declarative workflow summary")
    show_parser.add_argument("file", metavar="WORKFLOW_YAML")


# ── Dispatch ─────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    """Dispatch to the appropriate workflow subcommand."""
    cmd = args.workflow_subcommand
    if cmd == "init":
        return _run_init(args)
    elif cmd == "list-templates":
        return _run_list_templates(args)
    elif cmd in ("validate", "show"):
        return _run_validate(args, show=cmd == "show")
    print(f"error: unknown workflow subcommand '{cmd}'", file=sys.stderr)
    return 2


# ── Implementations ──────────────────────────────────────────────────


def _run_init(args: argparse.Namespace) -> int:
    """Copy and fill the workflow template."""
    variant = args.template or "workflow"

    # Locate template
    try:
        tpl = _template_path(variant)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    # Determine output path
    out = Path(args.output).expanduser().resolve()
    if out.exists():
        print(f"✗ {out} already exists — remove it first or use --output", file=sys.stderr)
        return 1

    interactive = sys.stdin.isatty() and not args.non_interactive

    # Gather values (flag → prompt → default)
    def val(flag_val: str, label: str, default: str = "") -> str:
        if flag_val:
            return flag_val
        if interactive:
            return _prompt(label, default)
        return default

    # Resolve tracker kind: flag → prompt (registry-validated) → template default
    from orchestratord.tracker_kinds import (
        SUPPORTED_TRACKERS,
        TrackerConfigError,
        normalize_tracker_kind,
        tracker_kind_info,
    )

    compat = _compatible_kinds(variant)
    kind_default = "github" if "github" in compat else (sorted(compat)[0] if compat else "github")

    if args.kind:
        kind = args.kind
    elif interactive:
        # Unified interactive init: every registry kind is offered and the
        # template variant is switched to the kind's canonical one below,
        # so there is no separate workflow/workflow-local entry point.
        options = sorted(SUPPORTED_TRACKERS - {"local"}) + ["local"]
        while True:
            raw = _prompt(f"Tracker kind ({'/'.join(options)})", kind_default)
            kind = raw.strip().lower() or kind_default
            if kind in SUPPORTED_TRACKERS:
                break
            print(
                f"    ✗ Unknown tracker kind '{kind}' — choose one of: {', '.join(options)}",
                file=sys.stderr,
            )
    else:
        kind = kind_default

    try:
        kind = normalize_tracker_kind(kind)
    except TrackerConfigError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    # A kind outside the template's compatibility set switches to the kind's
    # canonical template instead of failing (e.g. picking `local` under the
    # default remote-tracker template).
    if compat and kind not in compat:
        suggested = _template_variant_for_kind(kind)
        try:
            tpl = _template_path(suggested)
        except FileNotFoundError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        print(
            f"ℹ Template '{variant}' does not support tracker kind '{kind}' — "
            f"using '{suggested}' instead."
        )
        variant = suggested

    # Registry metadata drives prompts, clone domains, token env and endpoints
    info = tracker_kind_info(kind)
    clone_base = info.default_clone_base_url  # None for linear/local

    # Repository-hosted trackers need owner/repo/endpoint; others skip them
    if clone_base:
        owner = val(args.owner, "Upstream repository owner")
        repo = val(args.repo, "Repository name")
        endpoint = val(args.endpoint, "API endpoint (leave blank for default)")
    else:
        owner, repo, endpoint = args.owner, args.repo, args.endpoint
    assignee = val(args.assignee, "Issue assignee (leave blank for all)")
    branch_prefix = val(args.branch_prefix, "Branch prefix", "orchestratord")
    ws_root = val(args.workspace_root, "Workspace root", "/tmp/orchestratord_workspaces/myproject")

    # Build clone_url
    clone_url = ""
    push_user = ""
    if clone_base and owner and repo:
        clone_url = f"{clone_base}/{owner}/{repo}.git"
        push_user = owner

    # Fork 工作流：--owner/--repo 是上游，--fork-owner 是 fork 方
    #   repo_clone_url    = fork 仓库 (clone/push)  → {clone_base}/fork_owner/repo.git
    #   upstream_clone_url = 上游仓库 (PR 目标)      → {clone_base}/owner/repo.git
    #   --fork-owner 为空或与 --owner 相同 → 两者相同，退化为单仓库模式
    upstream_clone_url = clone_url  # 上游 URL，始终由 --owner/--repo 拼接
    if clone_base and repo:
        fork_owner = args.fork_owner
        if not fork_owner and interactive:
            fork_owner = _prompt(
                "Fork owner (leave blank for single-repo mode)",
                "",
            ).strip()
        if fork_owner and fork_owner != owner:
            clone_url = f"{clone_base}/{fork_owner}/{repo}.git"
        else:
            clone_url = upstream_clone_url  # 单仓库模式：两者相同

    # Token env var: registry's first API-key env; fallback covers local trackers
    token_env = info.api_key_env_vars[0] if info.api_key_env_vars else "TRACKER_API_KEY"

    # Tracker endpoint: registry default when not provided
    if not endpoint:
        endpoint = info.default_endpoint or ""

    # Local-tracker placeholders: prompt only when kind is local; remote
    # templates don't contain these <KEY> placeholders, so defaults are no-ops
    if kind == "local":
        issues_path = val("", "Issues path (local tracker)", ".issues")
        review_remote = val("", "Review remote name", "origin")
        review_prefix = val("", "Review branch prefix", "review")
        test_command = val("", "Test command (empty to skip)", "")
    else:
        issues_path, review_remote, review_prefix, test_command = (
            ".issues", "origin", "review", "",
        )

    # Build substitution map (covers both {{KEY}} and <KEY> placeholder styles)
    values = {
        "TRACKER_KIND": kind,
        "TRACKER_ENDPOINT": endpoint,
        "REPO_OWNER": owner,
        "REPO_NAME": repo,
        "REPO_CLONE_URL": clone_url,
        "UPSTREAM_CLONE_URL": upstream_clone_url,
        "TRACKER_API_KEY_ENV": token_env,
        "REPO_ASSIGNEE": assignee,
        "BRANCH_PREFIX": branch_prefix,
        "WORKSPACE_ROOT": ws_root,
        "GIT_PUSH_USER": push_user,
        "GIT_PUSH_TOKEN_ENV": token_env,
        "REPO_URL": clone_url.removesuffix(".git") if clone_url else "",
        # Keys for <KEY> style placeholders in workflow-local.template.md
        "OWNER": owner,
        "REPO": repo,
        "ISSUES_PATH": issues_path,
        "REVIEW_REMOTE": review_remote,
        "REVIEW_PREFIX": review_prefix,
        "TEST_COMMAND": test_command,
    }

    # Read and fill template
    raw = tpl.read_text(encoding="utf-8")
    filled = _fill_placeholders(raw, values)

    # Write
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(filled, encoding="utf-8")

    print(f"✓ Generated {out}")

    # ── workflow.yaml generation ───────────────────────────────────
    yaml_out = None
    if args.workflow_yaml:
        yaml_out = Path(args.workflow_yaml_output).expanduser().resolve()
        if yaml_out.exists():
            print(
                f"✗ {yaml_out} already exists — remove it first or use --workflow-yaml-output",
                file=sys.stderr,
            )
            return 1

        yaml_tpl_path = _template_path("workflow.yaml")
        yaml_raw = yaml_tpl_path.read_text(encoding="utf-8")

        # Add workflow YAML specific values
        yaml_values = values.copy()
        yaml_values["WORKFLOW_NAME"] = val(
            args.workflow_name if hasattr(args, "workflow_name") else "",
            "Workflow name",
            f"{repo or 'project'}-code-review",
        )
        yaml_values["PROJECT_NAME"] = repo or owner or "project"
        yaml_values["PROVIDER"] = "anthropic"
        yaml_values["MODEL"] = "claude-sonnet-4-20250514"

        yaml_filled = _fill_placeholders(yaml_raw, yaml_values)
        yaml_out.parent.mkdir(parents=True, exist_ok=True)
        yaml_out.write_text(yaml_filled, encoding="utf-8")
        print(f"✓ Generated {yaml_out}")

    print()
    print("  Next steps:")
    print(f"    1. Edit {out.name} — check every placeholder was replaced")
    if yaml_out:
        print(f"    2. Edit {yaml_out.name} — customize workflow stages for your project")
        print(f"    3. Set the required env var: export {token_env}=<your-token>")
        print(
            f"    4. Start: orchestratord server start --workflow {out.name} --workflow-yaml {yaml_out.name}"
        )
    else:
        print(f"    2. Set the required env var: export {token_env}=<your-token>")
        print(f"    3. Start: orchestratord server start --workflow {out.name}")
    print()
    if interactive:
        print("  Hint: re-run with --non-interactive and CLI flags to skip prompts.")
    return 0


def _run_list_templates(args: argparse.Namespace) -> int:
    """List all available template files."""
    try:
        templates = _available_templates()
    except Exception as exc:
        print(f"✗ Cannot list templates: {exc}", file=sys.stderr)
        return 1

    print("Available workflow templates:")
    print()
    for name, path in sorted(templates.items()):
        print(f"  {name:30s}  {path}")
    print()
    print("Usage:  orchestratord workflow init")
    return 0


def _run_validate(args: argparse.Namespace, *, show: bool = False) -> int:
    from orchestratord.workflow_engine.engine import WorkflowSchema, WorkflowSchemaError

    try:
        schema = WorkflowSchema.from_yaml(args.file)
        order = schema.build_dag_order()
    except (OSError, WorkflowSchemaError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if show:
        print(f"Workflow: {schema.name} (version {schema.version})")
        print(f"Stages: {len(schema.stages)}")
        for stage_id in order:
            stage = schema.get_stage(stage_id)
            if stage is not None:
                deps = ",".join(str(value) for value in stage.depends_on) or "-"
                print(f"  {stage.id}: {stage.name} [{stage.kind.value}] depends_on={deps}")
    else:
        print(f"✓ {args.file}: valid workflow ({len(schema.stages)} stages)")
    return 0
