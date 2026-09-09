'use client'

import { useState } from 'react'
import {
  useSendChatMessage,
  useSessionMessages,
  useSessions,
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
  const [newOpen, setNewOpen] = useState(false)
  const [contextOpen, setContextOpen] = useState(false)
  const sessions = useSessions(client, workspaceId)
  const start = useStartChatSession(client, workspaceId)
  const timeline = useSessionMessages(client, sessionId)
  const send = useSendChatMessage(client, sessionId ?? '')
  const stream = useChatStream(sessionId)
  const locale = useLocale()
  const c = chatCopy[locale]
  const chatSessions = (sessions.data ?? [])
    .filter((session) => session.origin.kind === 'direct')
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
  const selected = chatSessions.find((session) => session.id === sessionId)

  const openSession = (id: string) => { setSessionId(id); setNewOpen(false); setContextOpen(false) }
  const openNew = () => { setSessionId(null); setNewOpen(true); setContextOpen(false) }

  return (
    <div className={`chat-workspace${sessionId || newOpen ? ' chat-workspace--detail' : ''}${contextOpen ? ' chat-workspace--context' : ''}`}>
      <aside className="chat-threads" aria-label={c.conversations}>
        <header><div><p className="section-eyebrow">{c.recent}</p><h3>{c.conversations}</h3></div><Button size="sm" onClick={openNew}>{c.newChat}</Button></header>
        {sessions.isPending ? <p className="chat__empty">{c.loadingSessions}</p> : sessions.isError ? <p className="chat__error">{c.sessionsFailed}</p> : chatSessions.length === 0 ? <div className="chat-threads__empty"><p>{c.noConversations}</p><Button variant="secondary" onClick={openNew}>{c.start}</Button></div> : <div className="chat-threads__list">{chatSessions.map(session => <button key={session.id} data-selected={session.id === sessionId || undefined} onClick={() => openSession(session.id)}><strong>{c.session} {session.id.slice(0, 8)}</strong><span><i data-status={session.status} />{chatStatusLabel(session.status, locale)}</span><time>{new Date(session.created_at).toLocaleString(locale)}</time></button>)}</div>}
      </aside>
      <section className="chat chat-panel">
        {sessionId === null ? <>
          <header className="chat__header"><button className="chat-mobile-back" onClick={() => setNewOpen(false)}>← {c.conversations}</button><span className="chat__session">{c.newChat}</span></header>
          <div className="chat-start"><p className="chat__hint">{c.hint}</p><ChatComposer placeholder={c.describe} submitLabel={c.start} busy={start.isPending} onSend={(prompt) => start.mutate({ prompt }, { onSuccess: (data) => { setSessionId(data.session_id); setNewOpen(false) } })} />{start.isError && <p className="chat__error">{c.startFailed}: {start.error?.message ?? c.unknown}</p>}</div>
        </> : <>
          <header className="chat__header"><button className="chat-mobile-back" onClick={() => { setSessionId(null); setNewOpen(false) }}>← {c.conversations}</button><span className="chat__session">{c.session} {sessionId.slice(0, 8)}…</span><div><Button className="chat-context-trigger" size="sm" variant="ghost" onClick={() => setContextOpen(true)}>{c.context}</Button><Button size="sm" variant="ghost" onClick={openNew}>{c.newChat}</Button></div></header>
          {timeline.isPending ? <p className="chat__empty">{c.loading}</p> : timeline.isError ? <p className="chat__error">{c.loadFailed}: {timeline.error?.message ?? c.unknown}</p> : <ChatTimeline messages={timeline.data?.messages ?? []} />}
          {stream.streamText && <div className="chat__stream" role="status" aria-live="polite">{stream.streamText}</div>}
          {stream.streamError && <p className="chat__error">{stream.streamError}</p>}
          <ChatComposer placeholder={c.message} submitLabel={c.send} busy={send.isPending} onSend={(content) => send.mutate({ content })} />
          {send.isError && <p className="chat__error">{c.sendFailed}: {send.error?.message ?? c.unknown}</p>}
        </>}
      </section>
      <aside className="chat-context" aria-label={c.context}>
        <header><button className="chat-mobile-back" onClick={() => setContextOpen(false)}>← {c.conversation}</button><p className="section-eyebrow">{c.execution}</p><h3>{c.context}</h3></header>
        {selected ? <dl><div><dt>{c.status}</dt><dd>{chatStatusLabel(selected.status, locale)}</dd></div><div><dt>{c.mode}</dt><dd>{selected.mode}</dd></div><div><dt>{c.started}</dt><dd>{new Date(selected.created_at).toLocaleString(locale)}</dd></div><div><dt>ID</dt><dd><code>{selected.id}</code></dd></div></dl> : <p>{c.selectConversation}</p>}
        {selected && <a className="chat-context__link" href={`/sessions/${selected.id}`}>{c.openLedger} →</a>}
      </aside>
    </div>
  )
}

const chatCopy = {
  en: { hint: 'Start a conversation — a local agent session picks it up.', describe: 'Describe the task…', start: 'Start chat', startFailed: 'Could not start the conversation', unknown: 'unknown error', session: 'Session', newChat: 'New chat', loading: 'Loading messages…', loadFailed: 'Could not load messages', message: 'Message…', send: 'Send', sendFailed: 'Could not send the message', conversations: 'Conversations', conversation: 'Conversation', recent: 'RECENT THREADS', loadingSessions: 'Loading conversations…', sessionsFailed: 'Could not load conversations. Existing data is unchanged; check the local API and retry.', noConversations: 'No conversations yet.', context: 'Context', execution: 'EXECUTION', status: 'Status', mode: 'Mode', started: 'Started', openLedger: 'Open execution ledger', selectConversation: 'Select a conversation to inspect its execution context.' },
  'zh-CN': { hint: '开始对话，本地 Agent 会话将接手处理。', describe: '描述任务…', start: '开始对话', startFailed: '无法开始对话', unknown: '未知错误', session: '会话', newChat: '新对话', loading: '正在加载消息…', loadFailed: '无法加载消息', message: '输入消息…', send: '发送', sendFailed: '无法发送消息', conversations: '对话列表', conversation: '对话', recent: '最近对话', loadingSessions: '正在加载对话…', sessionsFailed: '无法加载对话。现有数据未改变；请检查本地 API 后重试。', noConversations: '暂无对话。', context: '上下文', execution: '执行信息', status: '状态', mode: '模式', started: '开始时间', openLedger: '打开执行账本', selectConversation: '选择一个对话以检查其执行上下文。' },
  ja: { hint: '会話を開始すると、ローカル Agent セッションが引き継ぎます。', describe: 'タスクを説明…', start: 'チャットを開始', startFailed: '会話を開始できませんでした', unknown: '不明なエラー', session: 'セッション', newChat: '新しいチャット', loading: 'メッセージを読み込み中…', loadFailed: 'メッセージを読み込めませんでした', message: 'メッセージ…', send: '送信', sendFailed: 'メッセージを送信できませんでした', conversations: '会話一覧', conversation: '会話', recent: '最近のスレッド', loadingSessions: '会話を読み込み中…', sessionsFailed: '会話を読み込めませんでした。既存データは変更されていません。ローカル API を確認してください。', noConversations: '会話はまだありません。', context: 'コンテキスト', execution: '実行情報', status: '状態', mode: 'モード', started: '開始日時', openLedger: '実行台帳を開く', selectConversation: '会話を選択すると実行コンテキストを確認できます。' },
} as const

const CHAT_STATUS = {
  en: { pending: 'Pending', queued: 'Queued', running: 'Running', paused: 'Paused', waiting: 'Waiting', completed: 'Completed', failed: 'Failed', stopped: 'Stopped', unknown: 'Unknown' },
  'zh-CN': { pending: '待处理', queued: '排队中', running: '运行中', paused: '已暂停', waiting: '等待中', completed: '已完成', failed: '失败', stopped: '已停止', unknown: '未知' },
  ja: { pending: '保留中', queued: '待機中', running: '実行中', paused: '一時停止', waiting: '待機中', completed: '完了', failed: '失敗', stopped: '停止済み', unknown: '不明' },
} as const

function chatStatusLabel(status: string, locale: keyof typeof CHAT_STATUS): string {
  return CHAT_STATUS[locale][status as keyof typeof CHAT_STATUS.en] ?? CHAT_STATUS[locale].unknown
}
