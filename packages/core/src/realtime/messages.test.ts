import { describe, expect, it } from 'vitest'
import { invalidationFor } from './messages'

describe('invalidationFor', () => {
  it('maps issue events to the workspace issue query', () => {
    expect(
      invalidationFor({ type: 'event', topic: 'issue.123', payload: {} }, 'ws-1'),
    ).toEqual([['issues', 'ws-1']])
  })

  it('maps session events to the sessions query', () => {
    expect(
      invalidationFor({ type: 'event', topic: 'session.abc' }, 'ws-1'),
    ).toEqual([['sessions']])
  })

  it('maps chat events to the per-session chat-messages query', () => {
    expect(
      invalidationFor(
        { type: 'event', topic: 'chat.d9b0f2a1-0000-0000-0000-000000000001' },
        'ws-1',
      ),
    ).toEqual([['chat-messages', 'd9b0f2a1-0000-0000-0000-000000000001']])
  })

  it('does not conflate chat topics with session topics', () => {
    // `chat.` and `session.` are distinct namespaces; the chat branch must
    // not shadow (or be shadowed by) the sessions mapping.
    expect(
      invalidationFor({ type: 'event', topic: 'session.abc' }, 'ws-1'),
    ).toEqual([['sessions']])
    expect(
      invalidationFor({ type: 'event', topic: 'chat.abc' }, 'ws-1'),
    ).toEqual([['chat-messages', 'abc']])
  })

  it('maps inbox created/resolved to the inbox query', () => {
    expect(invalidationFor({ type: 'inbox.created' }, 'ws-1')).toEqual([
      ['inbox', 'ws-1'],
    ])
    expect(invalidationFor({ type: 'inbox.resolved' }, 'ws-1')).toEqual([
      ['inbox', 'ws-1'],
    ])
  })

  it('maps agent capability changes to the agents query', () => {
    expect(
      invalidationFor({ type: 'agent.capability.changed' }, 'ws-1'),
    ).toEqual([['agents', 'ws-1']])
  })

  it('returns null for control frames', () => {
    expect(invalidationFor({ type: 'hello' }, 'ws-1')).toBeNull()
    expect(invalidationFor({ type: 'ping', ts: 1 }, 'ws-1')).toBeNull()
    expect(
      invalidationFor({ type: 'event', topic: 'unknown.42' }, 'ws-1'),
    ).toBeNull()
  })
})
