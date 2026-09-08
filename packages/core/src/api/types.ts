export type IssueStatus =
  | 'queued'
  | 'pending'
  | 'running'
  | 'pending_review'
  | 'completed'
  | 'failed'
  | 'abandoned'
  | 'verification_failed'

export interface InstanceBootstrap {
  instance_name: string
  workspace_id: string
  workspace_name: string
  server_version: string
  realtime_url: string
  features: Record<string, unknown>
}

export interface Issue {
  id: string
  workspace_id: string
  title: string
  description: string
  status: IssueStatus
  assignee_type: string | null
  assignee_id: string | null
  labels: string[]
  created_at: string
  /** Present only on the detail payload (`GET .../issues/{id}`). */
  comments?: IssueComment[]
}

export interface IssueComment {
  id: string
  issue_id: string
  author_type: string
  author_id: string
  body: string
  mentions: string[]
  created_at: string
}

export interface Session {
  id: string
  workspace_id: string
  issue_id: string | null
  agent_id: string | null
  run_id: string | null
  mode: string
  status: string
  created_at: string
}

export type SessionEventKind =
  | 'text'
  | 'text_delta'
  | 'tool_call'
  | 'tool_result'
  | 'turn_complete'
  | 'phase_complete'
  | 'session_complete'
  | 'error'
  | 'goal_set'
  | 'goal_status'
  | 'goal_continue'
  | 'goal_done'
  | 'goal_cleared'
  | 'goal_paused'
  | 'approval_request'
  | 'unknown'

export interface SessionEvent {
  seq: number
  timestamp: number
  kind: SessionEventKind
  payload: Record<string, unknown>
}

export interface SessionEventsPage {
  events: SessionEvent[]
  next_cursor: string | null
}

export interface Agent {
  id: string
  workspace_id: string
  name: string
  provider: string
  runtime_id: string
  capabilities_cache_jsonb: Record<string, boolean>
  created_at: string
}

export interface SkillSummary {
  name: string
  display_name: string
  description: string
  is_stale: boolean
  stale_reasons: string[]
}

export interface SkillSourceMapRef {
  claim: string
  file_path: string
  start_line: number
  end_line: number
  expected_sha256_prefix: string
}

export interface SkillDetail extends SkillSummary {
  skill_md: string
  source_map: SkillSourceMapRef[]
}

export interface UsageTotals {
  tokens_in: number
  tokens_out: number
  tokens_total: number
  cost_usd: number
  sessions: number
}

export interface UsageGroup extends UsageTotals {
  group: string
}

export interface UsageResponse {
  totals: UsageTotals
  groups: UsageGroup[]
}

export interface AgentUsageResponse extends UsageResponse {
  agent_id: string
}

export type InboxKind = 'approval_request' | 'clarification' | 'failed'

export type InboxItemStatus = 'open' | 'assigned' | 'resolved' | 'dismissed'

export interface InboxItem {
  id: string
  workspace_id: string
  kind: InboxKind
  title: string
  issue_id: string | null
  session_id: string | null
  event_seq: number | null
  status: InboxItemStatus
  assignee_type: string | null
  assignee_id: string | null
  created_at: string
}

export type RuntimeStatus = 'online' | 'offline' | 'disabled'

export interface RuntimeBackend {
  name: string
  version: string | null
}

export interface Runtime {
  id: string
  workspace_id: string
  hostname: string
  os: string
  status: RuntimeStatus
  last_seen_at: string | null
  created_at: string | null
  probed_backends: RuntimeBackend[]
}

export interface SquadMember {
  member_type: string
  member_id: string
}

export interface Squad {
  id: string
  workspace_id: string
  name: string
  leader_type: string
  leader_id: string
  members: SquadMember[]
  created_at: string | null
}

export interface ProjectRepo {
  repo_url: string
  default_branch: string
}

export interface ProjectDoc {
  doc_url: string
  doc_type: string
}

export interface Project {
  id: string
  workspace_id: string
  name: string
  description: string
  repos: ProjectRepo[]
  docs: ProjectDoc[]
}

export interface AutopilotRun {
  autopilot_id: string
  scheduled_at: string
  run_id: string
  status: string
  started_at: string | null
  finished_at: string | null
}

export interface Autopilot {
  id: string
  workspace_id: string
  name: string
  cron: string
  prompt: string
  target_kind: string
  target_id: string
  enabled: boolean
  /** Present only on the detail payload (`GET .../autopilots/{id}`). */
  runs?: AutopilotRun[]
}

export type MemberRole = 'owner' | 'admin' | 'member'

export interface Member {
  id: string
  workspace_id: string
  role: MemberRole
  name: string
  created_at: string | null
}

export interface MemberScopes {
  member_id: string
  agent_ids: string[]
}

export type AuditActorType = 'member' | 'agent' | 'system'

export interface AuditLogEntry {
  id: string
  workspace_id: string
  actor_type: AuditActorType
  actor_id: string
  action: string
  target_type: string
  target_id: string
  payload_jsonb: Record<string, unknown> | null
  created_at: string
}

export type PullRequestState = 'open' | 'closed' | 'merged'

export interface PullRequest {
  id: string
  issue_id: string | null
  repo: string
  number: number
  title: string
  state: string
  head_sha: string
  status: string
  created_at: string
  updated_at: string
}

export interface PullRequestsResponse {
  pull_requests: PullRequest[]
}

export type ChatRole = 'user' | 'assistant' | 'system' | 'tool'

export interface ChatMessage {
  id: string
  session_id: string
  seq: number
  role: ChatRole
  content: string
  agent_id: string | null
  author_label: string | null
  created_at: string
}

export interface ChatSessionMessages {
  session_id: string
  workspace_id: string
  messages: ChatMessage[]
}

export interface ChatSessionStart {
  session_id: string
  workspace_id: string
  status: string
  message: ChatMessage | null
}
