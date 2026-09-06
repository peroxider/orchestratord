/**
 * English copy — the canonical key source. `TranslationKey` is derived from this
 * object so that `zh-CN` is forced (at compile time) to provide every key.
 *
 * Flat dot-separated keys group by domain per the §5.6 terminology table
 * (apps/docs/content/docs/developers/conventions.mdx is the doc-of-record).
 */
export const en = {
  'issues.status.queued': 'Queued',
  'issues.status.pending': 'Pending',
  'issues.status.running': 'Running',
  'issues.status.pending_review': 'Pending review',
  'issues.status.completed': 'Completed',
  'issues.status.failed': 'Failed',
  'issues.status.abandoned': 'Abandoned',
  'issues.status.verification_failed': 'Verification failed',

  'inbox.kind.approval_request': 'Approval request',
  'inbox.kind.clarification': 'Clarification',
  'inbox.kind.failed': 'Failed',

  'inbox.status.open': 'Open',
  'inbox.status.assigned': 'Assigned',
  'inbox.status.resolved': 'Resolved',
  'inbox.status.dismissed': 'Dismissed',

  'members.role.owner': 'Owner',
  'members.role.admin': 'Admin',
  'members.role.member': 'Member',

  'runtimes.status.online': 'Online',
  'runtimes.status.offline': 'Offline',
  'runtimes.status.disabled': 'Disabled',

  'audit.actor.member': 'Member',
  'audit.actor.agent': 'Agent',
  'audit.actor.system': 'System',

  'usage.dimension.agent': 'Agent',
  'usage.dimension.backend': 'Backend',
  'usage.dimension.issue': 'Issue',
  'usage.dimension.day': 'Day',
  'usage.dimension.workspace': 'Workspace',

  'events.kind.text': 'Text',
  'events.kind.text_delta': 'Text delta',
  'events.kind.tool_call': 'Tool call',
  'events.kind.tool_result': 'Tool result',
  'events.kind.turn_complete': 'Turn complete',
  'events.kind.phase_complete': 'Phase complete',
  'events.kind.session_complete': 'Session complete',
  'events.kind.error': 'Error',
  'events.kind.goal_set': 'Goal set',
  'events.kind.goal_status': 'Goal status',
  'events.kind.goal_continue': 'Goal continue',
  'events.kind.goal_done': 'Goal done',
  'events.kind.goal_cleared': 'Goal cleared',
  'events.kind.goal_paused': 'Goal paused',
  'events.kind.approval_request': 'Approval request',
  'events.kind.unknown': 'Unknown',

  'events.summary.tool_result': 'tool result',
  'events.summary.approval_request': 'approval request',
  'events.summary.error': 'error',

  'agents.capability.streaming_deltas': 'Streaming deltas',
  'agents.capability.resumable': 'Resumable',
  'agents.capability.interrupt': 'Interrupt',
  'agents.capability.approval_hooks': 'Approval hooks',
  'agents.capability.parallel_sessions': 'Parallel sessions',
  'agents.capability.cost_reporting': 'Cost reporting',
  'agents.capability.tool_filtering': 'Tool filtering',
  'agents.capability.takeover': 'Takeover',
  'agents.capability.goal_mode': 'Goal mode',
  'agents.capability.resume_detection': 'Resume detection',

  'vcs.pr_state.open': 'Open',
  'vcs.pr_state.merged': 'Merged',
  'vcs.pr_state.closed': 'Closed',
}

export type TranslationKey = keyof typeof en
