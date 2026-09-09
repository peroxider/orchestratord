'use client'

import { useEffect, useState } from 'react'

import {
  useSession,
  useSessionEvents,
  useSessionControl,
  useSessionDecision,
} from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'
import type { BadgeTone } from '@orchestratord/ui'
import { ModeRenderer } from './mode-renderer'
import { useLocale } from '../i18n'

export interface SessionDetailProps {
  client: ApiClient
  sessionId: string
  resolveSource?: (session: NonNullable<ReturnType<typeof useSession>['data']>, locale: 'en' | 'zh-CN' | 'ja') => string
}

const STATUS_TONE: Record<string, BadgeTone> = {
  running: 'accent',
  paused: 'warn',
  stopped: 'neutral',
  completed: 'good',
  failed: 'bad',
}

export function SessionDetail({ client, sessionId, resolveSource }: SessionDetailProps) {
  const locale = useLocale()
  const c = sessionCopy[locale]
  const [requestedControl, setRequestedControl] = useState<'pause' | 'resume' | 'stop' | null>(null)
  const [controlTimedOut, setControlTimedOut] = useState(false)
  const session = useSession(client, sessionId)
  const events = useSessionEvents(client, sessionId)
  const approve = useSessionDecision(client, sessionId, 'approve')
  const deny = useSessionDecision(client, sessionId, 'deny')
  const pause = useSessionControl(client, sessionId, 'pause')
  const resume = useSessionControl(client, sessionId, 'resume')
  const stop = useSessionControl(client, sessionId, 'stop')
  const sessionStatus = session.data?.status
  const refetchSession = session.refetch

  useEffect(() => {
    if (!requestedControl || !sessionStatus) return
    const confirmed =
      (requestedControl === 'pause' && sessionStatus === 'paused') ||
      (requestedControl === 'resume' && sessionStatus === 'running') ||
      (requestedControl === 'stop' && ['stopped', 'completed', 'failed'].includes(sessionStatus))
    if (confirmed) {
      setRequestedControl(null)
      return
    }
    const poll = window.setInterval(() => void refetchSession(), 2_000)
    const timeout = window.setTimeout(() => {
      setRequestedControl(null)
      setControlTimedOut(true)
    }, 15_000)
    return () => {
      window.clearInterval(poll)
      window.clearTimeout(timeout)
    }
  }, [refetchSession, requestedControl, sessionStatus])

  if (session.isPending || events.isPending) {
    return <p className="session-detail__empty">{c.loading}</p>
  }
  if (session.isError || events.isError) {
    return (
      <p className="session-detail__empty">
        {c.failed}:{' '}
        {session.error?.message ?? events.error?.message ?? c.unknown}
      </p>
    )
  }

  const data = session.data
  const eventList = events.data?.events ?? []
  const approvals = eventList.filter((e) => e.kind === 'approval_request')
  const controlPending = pause.isPending || resume.isPending || stop.isPending || requestedControl !== null
  const controlError = pause.error ?? resume.error ?? stop.error

  const requestControl = (action: 'pause' | 'resume' | 'stop') => {
    setControlTimedOut(false)
    setRequestedControl(action)
    const mutation = action === 'pause' ? pause : action === 'resume' ? resume : stop
    mutation.mutate(undefined, { onError: () => setRequestedControl(null) })
  }

  return (
    <div className="session-detail">
      <nav className="detail-breadcrumb" aria-label={c.breadcrumb}><a href="/sessions">{c.sessions}</a><span>/</span><span aria-current="page">{data.id.slice(0, 8)}</span></nav>
      <header className="session-detail__header">
        <div className="session-detail__identity">
          <p className="section-eyebrow">EXECUTION / {data.mode.toUpperCase()}</p>
          <div><h2 className="session-detail__title">{c.session} {data.id.slice(0, 8)}</h2><Badge tone={STATUS_TONE[data.status] ?? 'neutral'}>{c.status[data.status as keyof typeof c.status] ?? data.status}</Badge></div>
        </div>
        <div className="session-detail__controls">
          {data.status === 'running' && (
            <Button size="sm" variant="secondary" disabled={controlPending} onClick={() => requestControl('pause')}>
              {requestedControl === 'pause' ? c.awaitPause : c.pause}
            </Button>
          )}
          {data.status === 'paused' && (
            <Button size="sm" variant="secondary" disabled={controlPending} onClick={() => requestControl('resume')}>
              {requestedControl === 'resume' ? c.awaitResume : c.resume}
            </Button>
          )}
          {(data.status === 'running' || data.status === 'paused') && (
            <Button size="sm" variant="danger" disabled={controlPending} onClick={() => { if (window.confirm(c.stopConfirm)) requestControl('stop') }}>
              {requestedControl === 'stop' ? c.awaitStop : c.stop}
            </Button>
          )}
        </div>
      </header>

      <dl className="session-detail__facts">
        <div><dt>{c.mode}</dt><dd>{data.mode}</dd></div>
        <div><dt>{c.source}</dt><dd>{resolveSource?.(data, locale) ?? (data.origin.kind === 'direct' ? c.direct : `${data.origin.source.kind} · ${data.origin.source.id}`)}</dd></div>
        <div><dt>Agent</dt><dd>{data.agent_id ? data.agent_id.slice(0, 8) : c.auto}</dd></div>
        <div><dt>{c.started}</dt><dd>{new Date(data.created_at).toLocaleString(locale)}</dd></div>
      </dl>

      {controlError && <p className="session-detail__control-error">{c.controlError}</p>}
      {controlTimedOut && <p className="session-detail__control-error">{c.controlTimeout}</p>}

      {approvals.length > 0 && (
        <div className="session-detail__approvals">
          {approvals.map((event) => (
            <div key={event.seq} className="session-detail__approval">
              <span className="session-detail__approval-label">
                {typeof event.payload.tool_name === 'string'
                  ? event.payload.tool_name
                  : c.approvalRequest}
              </span>
              <Button
                size="sm"
                variant="primary"
                disabled={approve.isPending || deny.isPending}
                onClick={() =>
                  approve.mutate(String(event.payload.request_id))
                }
              >
                {c.approve}
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={approve.isPending || deny.isPending}
                onClick={() => deny.mutate(String(event.payload.request_id))}
              >
                {c.deny}
              </Button>
            </div>
          ))}
        </div>
      )}

      <section className="session-detail__ledger">
        <header><div><p className="section-eyebrow">EXECUTION SPINE</p><h3>{c.ledger}</h3></div><span>{eventList.length} {c.events}</span></header>
        <ModeRenderer mode={data.mode} events={eventList} />
      </section>
    </div>
  )
}

const sessionCopy = {
  en: { loading: 'Loading session…', failed: 'Could not load session', unknown: 'unknown error', breadcrumb: 'Breadcrumb', sessions: 'Sessions', session: 'Session', status: { running: 'Running', paused: 'Paused', stopped: 'Stopped', completed: 'Completed', failed: 'Failed' }, pause: 'Pause', resume: 'Resume', stop: 'Stop', awaitPause: 'Awaiting pause…', awaitResume: 'Awaiting resume…', awaitStop: 'Awaiting stop…', stopConfirm: 'Stop this session and terminate its active child process?', mode: 'Mode', source: 'Source', issue: 'Issue', direct: 'Direct conversation', auto: 'Automatic routing', started: 'Started', controlError: 'The control request was not confirmed. The execution record is unchanged; check the Runtime connection and try again.', controlTimeout: 'The Runtime accepted the request but no authoritative state change arrived. The displayed status is unchanged; check the Runtime before retrying.', approvalRequest: 'approval request', approve: 'Approve', deny: 'Deny', ledger: 'Event ledger', events: 'events' },
  'zh-CN': { loading: '正在加载会话…', failed: '无法加载会话', unknown: '未知错误', breadcrumb: '面包屑导航', sessions: '会话', session: '会话', status: { running: '运行中', paused: '已暂停', stopped: '已停止', completed: '已完成', failed: '失败' }, pause: '暂停', resume: '继续', stop: '停止', awaitPause: '等待暂停确认…', awaitResume: '等待继续确认…', awaitStop: '等待停止确认…', stopConfirm: '停止此会话并终止其活跃子进程？', mode: '模式', source: '来源', issue: '任务', direct: '直接对话', auto: '自动路由', started: '开始时间', controlError: '控制请求尚未确认，执行记录没有改变；请检查运行时连接后重试。', controlTimeout: '运行时已接受请求，但尚未收到权威状态变化；当前状态保持不变，请检查运行时后再重试。', approvalRequest: '审批请求', approve: '批准', deny: '拒绝', ledger: '事件账本', events: '个事件' },
  ja: { loading: 'セッションを読み込み中…', failed: 'セッションを読み込めませんでした', unknown: '不明なエラー', breadcrumb: 'パンくず', sessions: 'セッション', session: 'セッション', status: { running: '実行中', paused: '一時停止', stopped: '停止済み', completed: '完了', failed: '失敗' }, pause: '一時停止', resume: '再開', stop: '停止', awaitPause: '一時停止を確認中…', awaitResume: '再開を確認中…', awaitStop: '停止を確認中…', stopConfirm: 'このセッションを停止し、実行中の子プロセスを終了しますか？', mode: 'モード', source: 'ソース', issue: 'Issue', direct: '直接会話', auto: '自動ルーティング', started: '開始日時', controlError: '制御要求を確認できませんでした。実行記録は変更されていません。ランタイム接続を確認して再試行してください。', controlTimeout: 'ランタイムは要求を受け付けましたが、確定状態が届いていません。表示中の状態は変更されていません。', approvalRequest: '承認要求', approve: '承認', deny: '拒否', ledger: 'イベント台帳', events: '件のイベント' },
} as const
