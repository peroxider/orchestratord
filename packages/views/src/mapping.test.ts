import { describe, expect, it } from 'vitest'
import type {
  AuditActorType,
  InboxItemStatus,
  PullRequestState,
  RuntimeStatus,
  SessionEventKind,
  UsageGroup,
} from '@orchestratord/core'
import { EVENT_TONE, eventKindLabel, eventSummary } from './sessions/event-kind'
import { ISSUE_STATUSES, STATUS_TONE, issueStatusLabel } from './issues/status'
import { capabilityLabels } from './agents/capability-labels'
import {
  USAGE_DIMENSIONS,
  dimensionLabel,
  formatTokens,
  formatUsd,
  toUsageCsv,
} from './usage/usage-labels'
import {
  INBOX_STATUS_TONE,
  inboxKindLabel,
  inboxStatusLabel,
} from './inbox/inbox-status'
import { RUNTIME_STATUS_TONE, runtimeStatusLabel } from './runtimes/runtime-status'
import { auditActorTypeLabel } from './audit/audit-labels'
import { PR_STATE_TONE, prStateLabel, prStateTone } from './vcs/pr-labels'
import { translate, type TranslationKey } from './i18n'
import { en } from './i18n/locales/en'
import { zhCN } from './i18n/locales/zh-CN'
import { cronSummary } from './autopilots/schedule'
import { redactSensitive } from './sessions/redact-sensitive'

describe('redactSensitive', () => {
  it('redacts sensitive keys at every nesting level without mutating safe evidence', () => {
    expect(redactSensitive({ command: 'deploy', token: 'abc123', nested: { api_key: 'key', value: 3 } })).toEqual({ command: 'deploy', token: '[REDACTED]', nested: { api_key: '[REDACTED]', value: 3 } })
  })

  it('redacts common inline credential forms', () => {
    expect(redactSensitive('Authorization: Bearer abcdefghijk token=secret-value sk-proj_abcdefgh')).not.toContain('abcdefghijk')
    expect(redactSensitive('Authorization: Bearer abcdefghijk token=secret-value sk-proj_abcdefgh')).toContain('[REDACTED]')
  })
})

describe('cronSummary', () => {
  it('turns common schedules into readable language', () => {
    expect(cronSummary('*/15 * * * *')).toBe('Every 15 minutes')
    expect(cronSummary('5 * * * *')).toBe('Hourly at :05')
    expect(cronSummary('30 8 * * *')).toBe('Daily at 08:30')
  })

  it('keeps custom schedules inspectable', () => {
    expect(cronSummary('0 9 * * 1')).toBe('Custom schedule · 0 9 * * 1')
    expect(cronSummary('invalid')).toBe('Invalid schedule')
  })
})

describe('eventSummary', () => {
  it('extracts text from text events', () => {
    expect(eventSummary({ kind: 'text', payload: { text: 'hello' } })).toBe('hello')
  })

  it('falls back to empty for missing text', () => {
    expect(eventSummary({ kind: 'text', payload: {} })).toBe('')
  })

  it('extracts tool name from tool_call', () => {
    expect(eventSummary({ kind: 'tool_call', payload: { name: 'read_file' } })).toBe('read_file')
  })

  it('falls back for tool_result without name', () => {
    expect(eventSummary({ kind: 'tool_result', payload: {} })).toBe('tool result')
  })

  it('extracts tool_name from approval_request', () => {
    expect(eventSummary({ kind: 'approval_request', payload: { tool_name: 'write_file' } })).toBe('write_file')
  })

  it('extracts message from error', () => {
    expect(eventSummary({ kind: 'error', payload: { message: 'boom' } })).toBe('boom')
  })

  it('returns the kind label for unhandled kinds', () => {
    expect(eventSummary({ kind: 'goal_set', payload: {} })).toBe('Goal set')
  })
})

describe('eventKindLabel', () => {
  it('humanizes event kinds in English', () => {
    expect(eventKindLabel('tool_call')).toBe('Tool call')
    expect(eventKindLabel('goal_set')).toBe('Goal set')
  })

  it('translates event kinds into Chinese', () => {
    expect(eventKindLabel('tool_call', 'zh-CN')).toBe('工具调用')
    expect(eventKindLabel('goal_set', 'zh-CN')).toBe('目标已设置')
  })
})

describe('EVENT_TONE', () => {
  it('covers every SessionEventKind', () => {
    const kinds: SessionEventKind[] = [
      'text',
      'text_delta',
      'tool_call',
      'tool_result',
      'turn_complete',
      'phase_complete',
      'session_complete',
      'error',
      'goal_set',
      'goal_status',
      'goal_continue',
      'goal_done',
      'goal_cleared',
      'goal_paused',
      'approval_request',
      'unknown',
    ]
    for (const kind of kinds) {
      expect(EVENT_TONE[kind]).toBeTruthy()
    }
  })
})

describe('ISSUE_STATUSES / STATUS_TONE', () => {
  it('lists the 8 display statuses in board order', () => {
    expect(ISSUE_STATUSES).toEqual([
      'queued',
      'pending',
      'running',
      'pending_review',
      'completed',
      'failed',
      'abandoned',
      'verification_failed',
    ])
  })

  it('has a tone for every status', () => {
    for (const status of ISSUE_STATUSES) {
      expect(STATUS_TONE[status]).toBeTruthy()
    }
  })

  it('labels statuses in title case and Chinese', () => {
    expect(issueStatusLabel('queued')).toBe('Queued')
    expect(issueStatusLabel('pending_review')).toBe('Pending review')
    expect(issueStatusLabel('queued', 'zh-CN')).toBe('排队中')
    expect(issueStatusLabel('verification_failed', 'zh-CN')).toBe('验证失败')
  })
})

describe('capabilityLabels', () => {
  it('declares all 10 canonical bits in order', () => {
    expect(capabilityLabels('en').map((c) => c.key)).toEqual([
      'streaming_deltas',
      'resumable',
      'interrupt',
      'approval_hooks',
      'parallel_sessions',
      'cost_reporting',
      'tool_filtering',
      'takeover',
      'goal_mode',
      'resume_detection',
    ])
  })

  it('translates capability labels', () => {
    expect(capabilityLabels('en').find((c) => c.key === 'streaming_deltas')?.label).toBe(
      'Streaming deltas',
    )
    expect(capabilityLabels('zh-CN').find((c) => c.key === 'streaming_deltas')?.label).toBe(
      '流式增量',
    )
  })
})

describe('USAGE_DIMENSIONS / dimensionLabel', () => {
  it('lists the grouping dimensions in selector order', () => {
    expect(USAGE_DIMENSIONS).toEqual(['agent', 'backend', 'issue', 'day', 'workspace'])
  })

  it('has a human label for every dimension', () => {
    for (const dim of USAGE_DIMENSIONS) {
      expect(dimensionLabel(dim)).toBeTruthy()
    }
  })

  it('labels a representative dimension', () => {
    expect(dimensionLabel('agent')).toBe('Agent')
    expect(dimensionLabel('day')).toBe('Day')
  })
})

describe('formatTokens / formatUsd', () => {
  it('formats tokens with thousands separators', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(1234567)).toBe('1,234,567')
  })

  it('formats USD to two decimals', () => {
    expect(formatUsd(0)).toBe('$0.00')
    expect(formatUsd(12.5)).toBe('$12.50')
    expect(formatUsd(1234.567)).toBe('$1234.57')
  })
})

describe('toUsageCsv', () => {
  it('emits a header only for empty groups', () => {
    expect(toUsageCsv([])).toBe(
      'group,tokens_in,tokens_out,tokens_total,cost_usd,sessions',
    )
  })

  it('emits one row per group', () => {
    const groups: UsageGroup[] = [
      {
        group: 'agent-a',
        tokens_in: 1,
        tokens_out: 2,
        tokens_total: 3,
        cost_usd: 0.5,
        sessions: 4,
      },
    ]
    expect(toUsageCsv(groups)).toBe(
      'group,tokens_in,tokens_out,tokens_total,cost_usd,sessions\nagent-a,1,2,3,0.5,4',
    )
  })
})

describe('INBOX_STATUS_TONE / inboxStatusLabel', () => {
  it('covers every InboxItemStatus', () => {
    const statuses: InboxItemStatus[] = ['open', 'assigned', 'resolved', 'dismissed']
    for (const status of statuses) {
      expect(INBOX_STATUS_TONE[status]).toBeTruthy()
      expect(inboxStatusLabel(status)).toBeTruthy()
    }
  })

  it('labels statuses in title case and Chinese', () => {
    expect(inboxStatusLabel('open')).toBe('Open')
    expect(inboxStatusLabel('resolved')).toBe('Resolved')
    expect(inboxStatusLabel('open', 'zh-CN')).toBe('待处理')
    expect(inboxStatusLabel('dismissed', 'zh-CN')).toBe('已忽略')
  })
})

describe('inboxKindLabel', () => {
  it('labels every inbox kind', () => {
    expect(inboxKindLabel('approval_request')).toBe('Approval request')
    expect(inboxKindLabel('clarification')).toBe('Clarification')
    expect(inboxKindLabel('failed')).toBe('Failed')
  })

  it('translates inbox kinds into Chinese', () => {
    expect(inboxKindLabel('approval_request', 'zh-CN')).toBe('审批请求')
    expect(inboxKindLabel('clarification', 'zh-CN')).toBe('澄清')
  })
})

describe('RUNTIME_STATUS_TONE / runtimeStatusLabel', () => {
  it('covers every RuntimeStatus', () => {
    const statuses: RuntimeStatus[] = ['online', 'offline', 'disabled']
    for (const status of statuses) {
      expect(RUNTIME_STATUS_TONE[status]).toBeTruthy()
      expect(runtimeStatusLabel(status)).toBeTruthy()
    }
  })

  it('labels statuses in title case and Chinese', () => {
    expect(runtimeStatusLabel('online')).toBe('Online')
    expect(runtimeStatusLabel('disabled')).toBe('Disabled')
    expect(runtimeStatusLabel('online', 'zh-CN')).toBe('在线')
    expect(runtimeStatusLabel('offline', 'zh-CN')).toBe('离线')
  })

  it('maps lifecycle colors', () => {
    expect(RUNTIME_STATUS_TONE.online).toBe('good')
    expect(RUNTIME_STATUS_TONE.offline).toBe('warn')
    expect(RUNTIME_STATUS_TONE.disabled).toBe('neutral')
  })
})

describe('auditActorTypeLabel', () => {
  it('labels every actor type', () => {
    const types: AuditActorType[] = ['member', 'agent', 'system']
    for (const type of types) {
      expect(auditActorTypeLabel(type)).toBeTruthy()
    }
  })

  it('maps to title-case labels', () => {
    expect(auditActorTypeLabel('member')).toBe('Local operator')
    expect(auditActorTypeLabel('agent')).toBe('Agent')
    expect(auditActorTypeLabel('system')).toBe('System')
  })

  it('translates actor types into Chinese', () => {
    expect(auditActorTypeLabel('member', 'zh-CN')).toBe('本地操作人')
    expect(auditActorTypeLabel('agent', 'zh-CN')).toBe('智能体')
  })
})

describe('PR_STATE_TONE / prStateLabel / prStateTone', () => {
  it('covers every PullRequestState', () => {
    const states: PullRequestState[] = ['open', 'closed', 'merged']
    for (const state of states) {
      expect(PR_STATE_TONE[state]).toBeTruthy()
    }
  })

  it('maps lifecycle tones', () => {
    expect(PR_STATE_TONE.open).toBe('good')
    expect(PR_STATE_TONE.merged).toBe('purple')
    expect(PR_STATE_TONE.closed).toBe('neutral')
  })

  it('labels states in title case', () => {
    expect(prStateLabel('open')).toBe('Open')
    expect(prStateLabel('merged')).toBe('Merged')
    expect(prStateLabel('closed')).toBe('Closed')
  })

  it('falls back safely for unknown states', () => {
    expect(prStateLabel('unknown')).toBe('unknown')
    expect(prStateTone('unknown')).toBe('neutral')
  })
})

describe('dictionary parity', () => {
  it('keeps zh-CN in 1:1 key parity with en', () => {
    expect(Object.keys(zhCN).sort()).toEqual(Object.keys(en).sort())
  })

  it('has a non-empty value for every key in both locales', () => {
    for (const key of Object.keys(en) as TranslationKey[]) {
      expect(en[key], key).toBeTruthy()
      expect(zhCN[key], key).toBeTruthy()
    }
  })
})

describe('translate', () => {
  it('resolves keys per locale', () => {
    expect(translate('en', 'issues.status.queued')).toBe('Queued')
    expect(translate('zh-CN', 'issues.status.queued')).toBe('排队中')
    expect(translate('zh-CN', 'events.kind.tool_call')).toBe('工具调用')
  })

  it('honors the fallback for unknown keys', () => {
    expect(translate('en', 'missing.key' as TranslationKey, 'fallback')).toBe('fallback')
  })
})
