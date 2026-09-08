import type { RealtimeMessage } from './messages'

const WS_OPEN = 1

export type RealtimeStatus = 'connecting' | 'open' | 'closed' | 'error'

export interface RealtimeClientOptions {
  url: string
  workspaceId: string
  token?: string
  WebSocketImpl?: typeof WebSocket
  onMessage?: (message: RealtimeMessage) => void
  onStatus?: (status: RealtimeStatus) => void
}

export class RealtimeClient {
  private readonly options: RealtimeClientOptions
  private socket: WebSocket | null = null
  private listeners = new Set<(message: RealtimeMessage) => void>()
  private topics = new Set<string>()

  constructor(options: RealtimeClientOptions) {
    this.options = options
  }

  connect(): void {
    const { url, workspaceId, token = '' } = this.options
    const params = new URLSearchParams({ workspace_id: workspaceId, token })
    const wsUrl = `${url}?${params.toString()}`
    const WsImpl = this.options.WebSocketImpl ?? WebSocket
    const socket = new WsImpl(wsUrl)
    this.socket = socket

    this.options.onStatus?.('connecting')
    socket.onopen = () => {
      this.options.onStatus?.('open')
      if (this.topics.size > 0) {
        this.send({ type: 'subscribe', topics: [...this.topics] })
      }
    }
    socket.onclose = () => this.options.onStatus?.('closed')
    socket.onerror = () => this.options.onStatus?.('error')
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(String(event.data)) as RealtimeMessage
        this.options.onMessage?.(message)
        for (const listener of this.listeners) {
          listener(message)
        }
      } catch {
        // ignore non-JSON frames
      }
    }
  }

  subscribe(topics: string[]): void {
    const fresh = topics.filter((topic) => !this.topics.has(topic))
    for (const topic of fresh) this.topics.add(topic)
    if (fresh.length > 0) this.send({ type: 'subscribe', topics: fresh })
  }

  unsubscribe(topics: string[]): void {
    const existing = topics.filter((topic) => this.topics.delete(topic))
    if (existing.length > 0) this.send({ type: 'unsubscribe', topics: existing })
  }

  /**
   * Fan out every received frame to *fn*; returns the unregister function.
   * Lets feature hooks (e.g. the chat stream) observe raw frames without
   * going through the query-invalidation bridge.
   */
  addMessageListener(fn: (message: RealtimeMessage) => void): () => void {
    this.listeners.add(fn)
    return () => {
      this.listeners.delete(fn)
    }
  }

  disconnect(): void {
    this.socket?.close()
    this.socket = null
  }

  private send(payload: object): void {
    if (this.socket?.readyState === WS_OPEN) {
      this.socket.send(JSON.stringify(payload))
    }
  }
}
