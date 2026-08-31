---
tracker:
  kind: "{{TRACKER_KIND}}"
  endpoint: "{{TRACKER_ENDPOINT}}"
  api_key: "${{TRACKER_API_KEY_ENV}}"
  owner: "{{REPO_OWNER}}"
  repo: "{{REPO_NAME}}"
  assignee: "{{REPO_ASSIGNEE}}"
  branch_prefix: "{{BRANCH_PREFIX}}"

polling:
  interval_ms: 30000

workspace:
  root: "{{WORKSPACE_ROOT}}"
  repo_clone_url: "{{REPO_CLONE_URL}}"
  upstream_clone_url: "{{UPSTREAM_CLONE_URL}}"

agent:
  max_concurrent_agents: 2
  max_turns: 100
  provider: anthropic

sandbox:
  approval_policy: never
---

# Issue-to-PR Agent Prompt

Implement the requested change, verify it, and leave the workspace ready for the
issue-to-PR application to synchronize. Do not push or create a pull request.
