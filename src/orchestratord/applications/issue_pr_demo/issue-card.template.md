---
# ============================================================================
# Local-Tracker Issue Card Template (companion to example.workflow.yaml)
# ============================================================================
#
# Used when tracker.kind = local. Save as `<ISSUES_PATH>/<FILE_STEM>.md`,
# where `<FILE_STEM>` matches the `id` field below.
#
# Frontmatter fields parsed by the local tracker adapter (see
# src/orchestratord/local_tracker/parser.py and tracker.py for the
# authoritative schema):
#
#   id              internal ID, unique within the issues directory.
#                   Conventionally "<FEATURE>-<SUB>", e.g. "agent-pr-007".
#   identifier      external identifier used in branch names and Jinja
#                   templates ({{ issue.identifier }}). Short and stable.
#   title           one-line title; surfaces in issue list and commits.
#   state           controls polling eligibility:
#                     open / ready      active, orchestrator will pull
#                     in_progress       claimed by a worker
#                     completed / closed / cancelled / failed / abandoned
#                                    terminal states — orchestrator skips
#                   See tracker.default_active_states_for_kind().
#   priority        int, lower = higher priority (0/1 = P0/P1). Optional.
#   labels          list[str]. Reserved labels:
#                     agent:retry       — reset and rerun
#                     agent:follow-up  — append commit on existing branch
#                     agent:blocked     — permanent skip (unblock to release)
#                     agent:rebase      — orchestrator rebase + force-push
#                   Plus ordinary category labels (feature/bug/refactor/...).
#   branch_name      workspace branch the agent checks out; default =
#                    "<branch_prefix>/<id>-<slug>" (git_sync._default_branch_name).
#   base_branch      workspace baseline; takes precedence over repo default.
#   assignee_id      owner/team (informational).
#   url              upstream link (issue tracker / docs).
#   created_at       ISO8601, e.g. 2026-08-31T10:00:00Z
#   updated_at       ISO8601.
# ============================================================================

id: <ID>                                # e.g. agent-pr-007
identifier: <IDENTIFIER>                # e.g. AGENT-7
title: <TITLE>                          # one-line summary
state: open                             # open | ready | in_progress | completed | closed | cancelled | failed | abandoned
priority: <0|1|2|3>                     # optional, lower = higher
labels:
  - feature                             # at least one category label
  - <CATEGORY_TAG>                      # e.g. review-auto-fix / docs / refactor
branch_name: <BRANCH_NAME>              # e.g. feature/pr-auto-fix; leave empty for auto
base_branch: <BASE_BRANCH>              # e.g. main
assignee_id: <ASSIGNEE>
url: <UPSTREAM_URL>                     # optional
created_at: <ISO8601>                   # e.g. 2026-08-31T10:00:00Z
updated_at: <ISO8601>                   # e.g. 2026-08-31T10:00:00Z
---

# <TITLE>

## Background

Why is this needed? Source: design doc / user feedback / perf data?

## Goal

One sentence: the observable behavior after completion.

## Sub-tasks

- [ ] sub-task 1
- [ ] sub-task 2
- [ ] sub-task 3

## Acceptance criteria

- executable check, e.g. `pytest tests/test_x.py::test_y` passes
- observable behavior in scenario X
- docs / CHANGELOG updated

## Risks & constraints

- compatibility impact
- performance / resource cost
- security / permission boundaries
- known uncovered edge cases

## Do NOT

- bundle unrelated cleanups
- refactor unrelated modules (file a separate one)
- touch core orchestration contracts (tracker.py / actions.py / engine.py)
- commit secrets, tokens, or PII

## References

- design doc: `<path>` §X
- related issues: `<other issue identifier>`
- related PRs: `<URL>`

## Notes

free-form: inspiration, references, follow-ups.