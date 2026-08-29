---
# Minimal local-tracker workflow for smoke-testing the orchestrator daemon.
tracker:
  kind: local
  issues_path: /tmp/orchestratord-test/issues
  assignee: test
  branch_prefix: feat
  active_states:
    - open
    - ready
  terminal_states:
    - completed
    - closed
    - cancelled
    - failed
    - abandoned

polling:
  interval_ms: 30000

workspace:
  root: /tmp/orchestratord-test

agent:
  max_concurrent_agents: 1
  max_turns: 50
  permission_mode: bypassPermissions
  provider: deepseek
  model: deepseek-v3

sandbox:
  turn_timeout_ms: 600000
---

# Orchestrator Agent Prompt

Smoke-test workflow. No issues are expected; the daemon must start and poll.
