# Issue → PR Application Demo (Layer 2)

This directory contains a **business-application example** showing how to
compose the generic orchestrator vocabulary (Layer 1, in
`src/orchestratord/templates/workflow.yaml.template`) into the bundled
Issue-to-PR scenario.

It is documentation + an annotated workflow definition — not a runnable
plugin. Action registration is the job of an installed plugin.

## Files

| File | Purpose |
| --- | --- |
| `example.workflow.yaml` | Declarative DAG composing generic stages with namespaced business actions |
| `issue-card.template.md` | Local-tracker issue card (used when `tracker.kind = local`) |

The companion `applications/issue_pr/` package (the importable
`IssueToPrApplication` class in `app.py`) is the historical Python shim — it
remains for compatibility; new code should use the declarative form here.

## What this example does

A linear-ish DAG with one refinement loop:

```
[1. fetch-issue]       ┐
                       ├──►[3. gate: review-intake]──►[5. decision]──►[7. pr-create]
[2. fetch-repo-context]┘                                │
                                                       └─refine→[4. implement]──►[6. gate: ci]──►[7.]
```

| Stage | Kind | Notes |
| --- | --- | --- |
| 1. fetch-issue | `action` | `uses: issue_pr.tracker.fetch` |
| 2. fetch-repo-context | `action` | `uses: issue_pr.workspace.clone` (fan-out runs concurrently with stage 1) |
| 3. review-intake | `gate` (manual) | Operator approves before agent runs |
| 4. implement | `agent` | Implements the change; writes `changes_summary.md` |
| 5. ship-or-refine | `decision` | Branches on agent outcome; `refine` loops back to stage 4 up to 3× |
| 6. ci-gate | `gate` (auto) | Runs ruff + pytest; rejects with rollback on failure |
| 7. pr-create | `action` | `uses: issue_pr.pr.create` |

## How to wire the namespaced actions

The example references three action names that are **not** registered by
the orchestrator core. Pick one of two registration paths.

### Option A — install a plugin (recommended)

Install a package that declares the entry points in its `pyproject.toml`:

```toml
[project.entry-points."orchestratord.actions"]
issue_pr.tracker.fetch   = "my_pkg.issue_pr:register_tracker_actions"
issue_pr.workspace.clone = "my_pkg.issue_pr:register_workspace_actions"
issue_pr.pr.create       = "my_pkg.issue_pr:register_pr_actions"
```

The discovery mechanism is in
`src/orchestratord/workflow_engine/actions.py:42-50` — it loads
`importlib.metadata` entry points under the `orchestratord.actions`
group at the first `resolve_action()` call.

### Option B — register in-process

For experiments, call `register_action()` at process startup before the
workflow engine loads:

```python
from orchestratord.workflow_engine import register_action

def fetch(config, context):
    # ... actual implementation reading workflow.md's tracker block ...
    return ActionResult(success=True, outputs=[...])

register_action("issue_pr.tracker.fetch", fetch)
```

Note: in-process registrations do **not** persist across processes. They
are suitable for tests and one-shot scripts, not for the long-running
daemon.

## How to run it locally

The example is **not directly runnable** without the actions registered.
To exercise the DAG shape:

```bash
# 1. Validate the workflow parses (no execution):
orchestratord workflow show example.workflow.yaml

# 2. Lint the YAML structure (catches schema violations):
python -c "from orchestratord.workflow_engine import WorkflowSchema; \
          print(WorkflowSchema.from_yaml('example.workflow.yaml').name)"
```

When the plugin is installed and the legacy `workflow.md` is in place:

```bash
orchestratord daemon start \
  --workflow example.workflow.yaml \
  --backend codex
```

## Layered design — what this directory is NOT

- It does **not** register any actions. The core stays decoupled.
- It does **not** define a `tracker:` / `workspace:` / `agent:` block.
  Those are daemon-level concerns living in legacy `workflow.md`.
- It does **not** shadow or replace `applications/issue_pr/`.
  That module's `IssueToPrApplication` class is preserved for import
  compatibility; new compositions use this declarative form.

## Where to extend

| Want to ... | Edit / Add |
| --- | --- |
| Add a new stage | `example.workflow.yaml` |
| Add a new validator | `workflow_engine/validators/` (core) |
| Add a new action kind | register via Option A or B above |
| Add a new business application | new directory under `applications/` mirroring this one |
| Change how stages are routed by issue content | `modes:` block in legacy `workflow.md` |

See also:

- `src/orchestratord/templates/workflow.yaml.template` — Layer 1 generic
  orchestrator vocabulary (stages, gates, decisions, validators).
- `src/orchestratord/templates/workflow.template.md` — legacy frontmatter
  config (tracker, polling, agent, sandbox, modes).
- `src/orchestratord/workflow_engine/actions.py` — action registration
  protocol.
- `src/orchestratord/workflow_engine/engine.py` — DAG executor.