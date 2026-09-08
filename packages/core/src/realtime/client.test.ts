import { beforeEach, describe, expect, it } from 'vitest'
import { RealtimeClient } from './client'

class MockWebSocket {
  static instances: MockWebSocket[] = []
  url: string
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null

  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.readyState = 3
    this.onclose?.()
  }
}

function lastInstance(): MockWebSocket {
  const inst = MockWebSocket.instances[MockWebSocket.instances.length - 1]
  if (!inst) throw new Error('no socket created')
  return inst
}

function newClient(
  options: Omit<
    ConstructorParameters<typeof RealtimeClient>[0],
    'url' | 'workspaceId' | 'WebSocketImpl'
  >,
) {
  return new RealtimeClient({
    url: 'ws://localhost:9000/ws',
    workspaceId: 'ws-1',
    WebSocketImpl: MockWebSocket as unknown as typeof WebSocket,
    ...options,
  })
}

describe('RealtimeClient', () => {
  beforeEach(() => {
    MockWebSocket.instances = []
  })

  it('connects with workspace_id and token query params', () => {
    const client = newClient({ token: 'dev' })
    client.connect()
    expect(lastInstance().url).toBe(
      'ws://localhost:9000/ws?workspace_id=ws-1&token=dev',
    )
  })

  it('subscribes to topics once the socket is open', () => {
    const client = newClient({})
    client.connect()
    const socket = lastInstance()
    socket.readyState = 1
    client.subscribe(['issue.123'])
    expect(socket.sent).toEqual([
      JSON.stringify({ type: 'subscribe', topics: ['issue.123'] }),
    ])
  })

  it('flushes subscriptions requested while connecting', () => {
    const client = newClient({})
    client.connect()
    const socket = lastInstance()
    client.subscribe(['chat.abc'])
    expect(socket.sent).toEqual([])
    socket.readyState = 1
    socket.onopen?.()
    expect(socket.sent).toEqual([
      JSON.stringify({ type: 'subscribe', topics: ['chat.abc'] }),
    ])
  })

  it('unsubscribes from topics', () => {
    const client = newClient({})
    client.connect()
    const socket = lastInstance()
    socket.readyState = 1
    client.subscribe(['chat.abc'])
    socket.sent = []
    client.unsubscribe(['chat.abc'])
    expect(socket.sent).toEqual([
      JSON.stringify({ type: 'unsubscribe', topics: ['chat.abc'] }),
    ])
  })

  it('fans frames out to message listeners and honors cleanup', () => {
    const seen: unknown[] = []
    const client = newClient({})
    client.connect()
    const detach = client.addMessageListener((m) => seen.push(m))
    lastInstance().onmessage?.({
      data: JSON.stringify({ type: 'event', topic: 'chat.1' }),
    })
    expect(seen).toEqual([{ type: 'event', topic: 'chat.1' }])

    detach()
    lastInstance().onmessage?.({
      data: JSON.stringify({ type: 'event', topic: 'chat.2' }),
    })
    expect(seen).toEqual([{ type: 'event', topic: 'chat.1' }])
  })

  it('dispatches parsed messages to onMessage', () => {
    const messages: unknown[] = []
    const client = newClient({ onMessage: (m) => messages.push(m) })
    client.connect()
    lastInstance().onmessage?.({
      data: JSON.stringify({ type: 'event', topic: 'issue.1' }),
    })
    expect(messages).toEqual([{ type: 'event', topic: 'issue.1' }])
  })

  it('ignores non-JSON frames', () => {
    const messages: unknown[] = []
    const client = newClient({ onMessage: (m) => messages.push(m) })
    client.connect()
    lastInstance().onmessage?.({ data: 'not json' })
    expect(messages).toEqual([])
  })

  it('closes the socket on disconnect', () => {
    const client = newClient({})
    client.connect()
    const socket = lastInstance()
    client.disconnect()
    expect(socket.readyState).toBe(3)
  })
})
