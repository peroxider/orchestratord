import { describe, expect, it } from 'vitest'
import { inboxApprovalRequestId } from './inbox'

const events = [
  { seq: 1, timestamp: 1, kind: 'text' as const, payload: {} },
  { seq: 3, timestamp: 2, kind: 'approval_request' as const, payload: { request_id: 'req-3' } },
  { seq: 5, timestamp: 3, kind: 'approval_request' as const, payload: { request_id: 'req-5' } },
]

describe('inboxApprovalRequestId', () => {
  it('resolves the exact linked approval event', () => expect(inboxApprovalRequestId(events, 3)).toBe('req-3'))
  it('falls back to the latest approval and rejects malformed events', () => {
    expect(inboxApprovalRequestId(events, null)).toBe('req-5')
    expect(inboxApprovalRequestId(events, 1)).toBeNull()
  })
})
