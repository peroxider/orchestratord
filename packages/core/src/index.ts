export { ApiClient, ApiError } from './api/client'
export type { ApiClientOptions } from './api/client'
export type {
  ApplicationBootstrap,
  InstanceBootstrap,
  ConversationOrigin,
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
  AuditLogEntry,
  AuditActorType,
  ChatRole,
  ChatMessage,
  ChatSessionMessages,
  ChatSessionStart,
} from './api/types'
export {
  useSessions,
  useSession,
  useSessionEvents,
  useSessionDecision,
  useSessionControl,
} from './queries/sessions'
export { useInstance } from './queries/instance'
export {
  useSessionMessages,
  useStartChatSession,
  useSendChatMessage,
} from './queries/chat'
export type {
  StartChatSessionInput,
  SendChatMessageInput,
} from './queries/chat'
export { useAgents, useAgent } from './queries/agents'
export { useSkills, useSkill, useVerifySkill } from './queries/skills'
export type { SkillVerifyResult } from './queries/skills'
export { useWorkspaceUsage, useAgentUsage } from './queries/usage'
export type { UsageDimension, UsageWindow } from './queries/usage'
export {
  useInbox,
  useAssignInbox,
  useResolveInbox,
  useDismissInbox,
  useInboxDecision,
  inboxApprovalRequestId,
  useAnswerInboxClarification,
} from './queries/inbox'
export {
  useRuntimes,
  useRuntime,
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
export { useAudit } from './queries/audit'
export type { AuditFilters } from './queries/audit'
export { adaptSession, adaptInboxItem, adaptAuditEntry } from './api/adapters'
export { CoreProvider } from './provider'
export * from './realtime'
