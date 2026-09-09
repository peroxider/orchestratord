export interface RealtimeMessage {
  type: string
  topic?: string
  [key: string]: unknown
}

/**
 * Map a server → client realtime frame to the TanStack Query keys it
 * invalidates (§5.4.3), or `null` for frames that carry no server-state
 * change (handshake, heartbeat, subscription acks).
 */
export function invalidationFor(
  message: RealtimeMessage,
  workspaceId: string,
): string[][] | null {
  switch (message.type) {
    case 'event': {
      const topic = typeof message.topic === 'string' ? message.topic : ''
      if (topic.startsWith('chat.')) {
        // Per-session chat timeline: streaming frames keep the streaming
        // bubble fresh; turn/session completion also triggers this refetch
        // so the persisted assistant message replaces the buffer.
        return [['chat-messages', topic.slice('chat.'.length)]]
      }
      if (topic.startsWith('session.')) {
        return [['sessions']]
      }
      return null
    }
    case 'inbox.created':
    case 'inbox.resolved':
      return [['inbox', workspaceId]]
    case 'agent.capability.changed':
      return [['agents', workspaceId]]
    default:
      return null
  }
}
