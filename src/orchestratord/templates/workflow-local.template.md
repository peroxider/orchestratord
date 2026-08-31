---
tracker:
  kind: local
  issues_path: "<ISSUES_PATH>"
  assignee: "<REPO_ASSIGNEE>"
  branch_prefix: "<BRANCH_PREFIX>"
  active_states: [open, ready]
  terminal_states: [completed, closed, cancelled, failed, abandoned]

polling:
  interval_ms: 30000

workspace:
  root: "<WORKSPACE_ROOT>"

agent:
  max_concurrent_agents: 1
  max_turns: 100

sandbox:
  approval_policy: never
---

# Local Issue-to-PR Agent Prompt

Implement and verify the issue in the prepared workspace. The application owns
branch synchronization and review publication.
