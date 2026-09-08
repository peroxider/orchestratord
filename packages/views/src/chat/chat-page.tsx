'use client'

import { useState } from 'react'
import {
  useSendChatMessage,
  useSessionMessages,
  useStartChatSession,
  type ApiClient,
} from '@orchestratord/core'
import { Button } from '@orchestratord/ui'
import { ChatComposer } from './chat-composer'
import { ChatTimeline } from './chat-timeline'
import { useChatStream } from './use-chat-stream'

export interface ChatPageProps {
  client: ApiClient
  workspaceId: string
}

export function ChatPage({ client, workspaceId }: ChatPageProps) {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const start = useStartChatSession(client, workspaceId)
  const timeline = useSessionMessages(client, sessionId)
  const send = useSendChatMessage(client, sessionId ?? '')
  const stream = useChatStream(sessionId)

  if (sessionId === null) {
    return (
      <div className="chat chat--start">
        <p className="chat__hint">
          Start a conversation — a workspace agent session picks it up.
        </p>
        <ChatComposer
          placeholder="Describe the task…"
          submitLabel="Start chat"
          busy={start.isPending}
          onSend={(prompt) =>
            start.mutate(
              { prompt },
              { onSuccess: (data) => setSessionId(data.session_id) },
            )
          }
        />
        {start.isError && (
          <p className="chat__error">
            Failed to start: {start.error?.message ?? 'unknown error'}
          </p>
        )}
      </div>
    )
  }

  return (
    <div className="chat">
      <header className="chat__header">
        <span className="chat__session">session {sessionId.slice(0, 8)}…</span>
        <Button size="sm" variant="ghost" onClick={() => setSessionId(null)}>
          New chat
        </Button>
      </header>
      {timeline.isPending ? (
        <p className="chat__empty">Loading messages…</p>
      ) : timeline.isError ? (
        <p className="chat__error">
          Failed to load messages: {timeline.error?.message ?? 'unknown error'}
        </p>
      ) : (
        <ChatTimeline messages={timeline.data?.messages ?? []} />
      )}
      {stream.streamText && (
        <div className="chat__stream" role="status" aria-live="polite">
          {stream.streamText}
        </div>
      )}
      {stream.streamError && <p className="chat__error">{stream.streamError}</p>}
      <ChatComposer busy={send.isPending} onSend={(content) => send.mutate({ content })} />
      {send.isError && (
        <p className="chat__error">
          Failed to send: {send.error?.message ?? 'unknown error'}
        </p>
      )}
    </div>
  )
}
