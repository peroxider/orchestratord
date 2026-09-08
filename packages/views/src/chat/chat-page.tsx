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
import { useLocale } from '../i18n'

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
  const c = chatCopy[useLocale()]

  if (sessionId === null) {
    return (
      <div className="chat chat--start">
        <p className="chat__hint">
          {c.hint}
        </p>
        <ChatComposer
          placeholder={c.describe}
          submitLabel={c.start}
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
            {c.startFailed}: {start.error?.message ?? c.unknown}
          </p>
        )}
      </div>
    )
  }

  return (
    <div className="chat">
      <header className="chat__header">
        <span className="chat__session">{c.session} {sessionId.slice(0, 8)}…</span>
        <Button size="sm" variant="ghost" onClick={() => setSessionId(null)}>
          {c.newChat}
        </Button>
      </header>
      {timeline.isPending ? (
        <p className="chat__empty">{c.loading}</p>
      ) : timeline.isError ? (
        <p className="chat__error">
          {c.loadFailed}: {timeline.error?.message ?? c.unknown}
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
      <ChatComposer placeholder={c.message} submitLabel={c.send} busy={send.isPending} onSend={(content) => send.mutate({ content })} />
      {send.isError && (
        <p className="chat__error">
          {c.sendFailed}: {send.error?.message ?? c.unknown}
        </p>
      )}
    </div>
  )
}

const chatCopy = {
  en: { hint: 'Start a conversation — a local agent session picks it up.', describe: 'Describe the task…', start: 'Start chat', startFailed: 'Could not start the conversation', unknown: 'unknown error', session: 'Session', newChat: 'New chat', loading: 'Loading messages…', loadFailed: 'Could not load messages', message: 'Message…', send: 'Send', sendFailed: 'Could not send the message' },
  'zh-CN': { hint: '开始对话，本地 Agent 会话将接手处理。', describe: '描述任务…', start: '开始对话', startFailed: '无法开始对话', unknown: '未知错误', session: '会话', newChat: '新对话', loading: '正在加载消息…', loadFailed: '无法加载消息', message: '输入消息…', send: '发送', sendFailed: '无法发送消息' },
  ja: { hint: '会話を開始すると、ローカル Agent セッションが引き継ぎます。', describe: 'タスクを説明…', start: 'チャットを開始', startFailed: '会話を開始できませんでした', unknown: '不明なエラー', session: 'セッション', newChat: '新しいチャット', loading: 'メッセージを読み込み中…', loadFailed: 'メッセージを読み込めませんでした', message: 'メッセージ…', send: '送信', sendFailed: 'メッセージを送信できませんでした' },
} as const
