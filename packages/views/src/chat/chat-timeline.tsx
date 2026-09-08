'use client'

import { useEffect, useRef } from 'react'
import type { ChatMessage } from '@orchestratord/core'
import { useLocale } from '../i18n'

export interface ChatTimelineProps {
  messages: ChatMessage[]
}

const ROLE_CLASS: Record<string, string> = {
  user: 'chat-msg--user',
  assistant: 'chat-msg--assistant',
  system: 'chat-msg--system',
  tool: 'chat-msg--system',
}

export function ChatTimeline({ messages }: ChatTimelineProps) {
  const endRef = useRef<HTMLDivElement>(null)
  const locale = useLocale()

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [messages.length])

  return (
    <div className="chat-timeline">
      {messages.length === 0 && (
        <p className="chat__empty">{{ en: 'No messages yet.', 'zh-CN': '暂无消息。', ja: 'メッセージはまだありません。' }[locale]}</p>
      )}
      {messages.map((m) => (
        <div key={m.id} className={`chat-msg ${ROLE_CLASS[m.role] ?? 'chat-msg--system'}`}>
          <div className="chat-msg__meta">
            <span className="chat-msg__author">
              {m.author_label ?? m.agent_id ?? m.role}
            </span>
            <span className="chat-msg__time">
              {new Date(m.created_at).toLocaleTimeString(locale)}
            </span>
          </div>
          <div className="chat-msg__body">{m.content}</div>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  )
}
