'use client'

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

export interface SessionDetailProps {
  client: ApiClient
  sessionId: string
}

const STATUS_TONE: Record<string, BadgeTone> = {
  running: 'accent',
  paused: 'warn',
  stopped: 'neutral',
  completed: 'good',
  failed: 'bad',
}

export function SessionDetail({ client, sessionId }: SessionDetailProps) {
  const session = useSession(client, sessionId)
  const events = useSessionEvents(client, sessionId)
  const approve = useSessionDecision(client, sessionId, 'approve')
  const deny = useSessionDecision(client, sessionId, 'deny')
  const pause = useSessionControl(client, sessionId, 'pause')
  const resume = useSessionControl(client, sessionId, 'resume')
  const stop = useSessionControl(client, sessionId, 'stop')

  if (session.isPending || events.isPending) {
    return <p className="session-detail__empty">Loading session…</p>
  }
  if (session.isError || events.isError) {
    return (
      <p className="session-detail__empty">
        Failed to load session:{' '}
        {session.error?.message ?? events.error?.message ?? 'unknown error'}
      </p>
    )
  }

  const data = session.data
  const eventList = events.data?.events ?? []
  const approvals = eventList.filter((e) => e.kind === 'approval_request')
  const controlPending = pause.isPending || resume.isPending || stop.isPending
  const controlError = pause.error ?? resume.error ?? stop.error

  return (
    <div className="session-detail">
      <header className="session-detail__header">
        <div className="session-detail__identity">
          <p className="section-eyebrow">EXECUTION / {data.mode.toUpperCase()}</p>
          <div><h2 className="session-detail__title">Session {data.id.slice(0, 8)}</h2><Badge tone={STATUS_TONE[data.status] ?? 'neutral'}>{data.status}</Badge></div>
        </div>
        <div className="session-detail__controls">
          {data.status === 'running' && (
            <Button size="sm" variant="secondary" disabled={controlPending} onClick={() => pause.mutate()}>
              Pause
            </Button>
          )}
          {data.status === 'paused' && (
            <Button size="sm" variant="secondary" disabled={controlPending} onClick={() => resume.mutate()}>
              Resume
            </Button>
          )}
          {(data.status === 'running' || data.status === 'paused') && (
            <Button size="sm" variant="danger" disabled={controlPending} onClick={() => { if (window.confirm('Stop this session and terminate its active child process?')) stop.mutate() }}>
              {stop.isPending ? 'Stopping…' : 'Stop'}
            </Button>
          )}
        </div>
      </header>

      <dl className="session-detail__facts">
        <div><dt>Mode</dt><dd>{data.mode}</dd></div>
        <div><dt>Source</dt><dd>{data.issue_id ? `Issue ${data.issue_id.slice(0, 8)}` : 'Direct conversation'}</dd></div>
        <div><dt>Agent</dt><dd>{data.agent_id ? data.agent_id.slice(0, 8) : 'Automatic routing'}</dd></div>
        <div><dt>Started</dt><dd>{new Date(data.created_at).toLocaleString()}</dd></div>
      </dl>

      {controlError && <p className="session-detail__control-error">The control request was not confirmed. The execution record is unchanged; check the Runtime connection and try again.</p>}

      {approvals.length > 0 && (
        <div className="session-detail__approvals">
          {approvals.map((event) => (
            <div key={event.seq} className="session-detail__approval">
              <span className="session-detail__approval-label">
                {typeof event.payload.tool_name === 'string'
                  ? event.payload.tool_name
                  : 'approval request'}
              </span>
              <Button
                size="sm"
                variant="primary"
                onClick={() =>
                  approve.mutate(String(event.payload.request_id))
                }
              >
                Approve
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => deny.mutate(String(event.payload.request_id))}
              >
                Deny
              </Button>
            </div>
          ))}
        </div>
      )}

      <section className="session-detail__ledger">
        <header><div><p className="section-eyebrow">EXECUTION SPINE</p><h3>Event ledger</h3></div><span>{eventList.length} events</span></header>
        <ModeRenderer mode={data.mode} events={eventList} />
      </section>
    </div>
  )
}
