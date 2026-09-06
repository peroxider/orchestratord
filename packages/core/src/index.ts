export { ApiClient, ApiError } from './api/client'
export type { ApiClientOptions } from './api/client'
export type {
  Issue,
  IssueComment,
  IssueStatus,
  Session,
  SessionEvent,
  SessionEventKind,
  SessionEventsPage,
  Agent,
  SkillSummary,
  SkillSourceMapRef,
  SkillDetail,
  UsageTotals,
  UsageGroup,
  UsageResponse,
  AgentUsageResponse,
  InboxItem,
  InboxKind,
  InboxItemStatus,
  Runtime,
  RuntimeBackend,
  RuntimeStatus,
  Squad,
  SquadMember,
  Project,
  ProjectRepo,
  ProjectDoc,
  Autopilot,
  AutopilotRun,
  Member,
  MemberRole,
  MemberScopes,
  AuditLogEntry,
  AuditActorType,
  PullRequest,
  PullRequestState,
  PullRequestsResponse,
} from './api/types'
export { useIssues, useIssue, useCreateIssue, useUpdateIssue, useAddComment, useMoveIssue } from './queries/issues'
export type {
  IssueFilters,
  CreateIssueInput,
  UpdateIssueInput,
  AddCommentInput,
} from './queries/issues'
export {
  useSessionsByIssue,
  useSession,
  useSessionEvents,
  useSessionDecision,
  useSessionControl,
} from './queries/sessions'
export { useAgents } from './queries/agents'
export { useSkills, useSkill, useVerifySkill } from './queries/skills'
export type { SkillVerifyResult } from './queries/skills'
export { useWorkspaceUsage, useAgentUsage } from './queries/usage'
export type { UsageDimension, UsageWindow } from './queries/usage'
export {
  useInbox,
  useAssignInbox,
  useResolveInbox,
  useDismissInbox,
} from './queries/inbox'
export {
  useRuntimes,
  useRegisterRuntime,
  useRevokeRuntime,
} from './queries/runtimes'
export type { RegisterRuntimeInput, RegisteredRuntime } from './queries/runtimes'
export {
  useSquads,
  useSquad,
  useCreateSquad,
  useDeleteSquad,
} from './queries/squads'
export type { CreateSquadInput } from './queries/squads'
export {
  useProjects,
  useProject,
  useCreateProject,
  useAddProjectRepo,
  useAddProjectDoc,
} from './queries/projects'
export type {
  CreateProjectInput,
  AddProjectRepoInput,
  AddProjectDocInput,
} from './queries/projects'
export {
  useAutopilots,
  useAutopilot,
  useCreateAutopilot,
  usePatchAutopilot,
} from './queries/autopilots'
export type { CreateAutopilotInput } from './queries/autopilots'
export {
  useMembers,
  useCreateMember,
  useUpdateMember,
  useDeleteMember,
  useMemberScopes,
  useGrantScope,
  useRevokeScope,
} from './queries/members'
export type { CreateMemberInput, UpdateMemberInput } from './queries/members'
export { useAudit } from './queries/audit'
export type { AuditFilters } from './queries/audit'
export { usePullRequests } from './queries/vcs'
export { useWorkspaceStore } from './store/workspace'
export type { WorkspaceState } from './store/workspace'
export { CoreProvider } from './provider'
export * from './realtime'
