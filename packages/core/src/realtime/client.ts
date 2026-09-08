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
    socket.onopen = () => this.options.onStatus?.('open')
    socket.onclose = () => this.options.onStatus?.('closed')
    socket.onerror = () => this.options.onStatus?.('error')
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(String(event.data)) as RealtimeMessage
        this.options.onMessage?.(message)
      } catch {
        // ignore non-JSON frames
      }
    }
  }

  subscribe(topics: string[]): void {
    this.send({ type: 'subscribe', topics })
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
