import { describe, expect, it } from 'vitest'
import { adaptAuditEntry, adaptInboxItem, adaptSession } from './adapters'

describe('legacy resource adapters', () => {
  it('normalizes a legacy issue session exactly once', () => {
    const session = adaptSession({ id: 's-1', workspace_id: 'w-1', issue_id: 'i-1', agent_id: null, run_id: null, mode: 'pipeline', status: 'running', created_at: '2026-09-09T00:00:00Z' })
    expect(session.source_ref).toEqual({ application_id: 'issue_pr', kind: 'issue', id: 'i-1' })
    expect(session.origin).toEqual({ kind: 'resource', source: session.source_ref })
    expect(session).not.toHaveProperty('issue_id')
  })

  it('preserves explicit future application references', () => {
    const ref = { application_id: 'sop_agent', kind: 'sop', id: 'sop-4' }
    const item = adaptInboxItem({ id: 'in-1', workspace_id: 'w-1', kind: 'clarification', title: 'Validate input', resource_ref: ref, session_ref: null, session_id: null, event_seq: null, status: 'open', assignee_type: null, assignee_id: null, created_at: '2026-09-09T00:00:00Z' })
    expect(item.application_id).toBe('sop_agent')
    expect(item.resource_ref).toEqual(ref)
  })

  it('creates an inspectable generic activity target', () => {
    const entry = adaptAuditEntry({ id: 'a-1', workspace_id: 'w-1', actor_type: 'system', actor_id: 'system', action: 'observed', target_type: 'future_kind', target_id: 'f-1', payload_jsonb: null, created_at: '2026-09-09T00:00:00Z' })
    expect(entry.target_ref).toEqual({ application_id: null, kind: 'future_kind', id: 'f-1' })
  })
})
