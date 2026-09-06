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
import { EventTimeline } from './event-timeline'

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

  return (
    <div className="session-detail">
      <header className="session-detail__header">
        <h2 className="session-detail__title">{data.mode} session</h2>
        <Badge tone={STATUS_TONE[data.status] ?? 'neutral'}>
          {data.status}
        </Badge>
        <div className="session-detail__controls">
          {data.status === 'running' && (
            <Button size="sm" variant="secondary" onClick={() => pause.mutate()}>
              Pause
            </Button>
          )}
          {data.status === 'paused' && (
            <Button size="sm" variant="secondary" onClick={() => resume.mutate()}>
              Resume
            </Button>
          )}
          {(data.status === 'running' || data.status === 'paused') && (
            <Button size="sm" variant="danger" onClick={() => stop.mutate()}>
              Stop
            </Button>
          )}
        </div>
      </header>

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

      <EventTimeline events={eventList} />
    </div>
  )
}
