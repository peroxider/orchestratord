'use client'

import { useEffect, useState } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { EVENT_TONE, eventKindLabel, eventSummary } from './event-kind'

export interface EventTimelineProps {
  events: SessionEvent[]
}

const SPEEDS = [0.5, 1, 2] as const

export function EventTimeline({ events }: EventTimelineProps) {
  const [visibleCount, setVisibleCount] = useState(events.length)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<number>(1)
  const { locale } = useTranslation()

  useEffect(() => {
    if (!playing) return
    if (visibleCount >= events.length) {
      setPlaying(false)
      return
    }
    const timer = setTimeout(() => setVisibleCount((n) => n + 1), 500 / speed)
    return () => clearTimeout(timer)
  }, [playing, visibleCount, speed, events.length])

  if (events.length === 0) {
    return <p className="timeline__empty">No events yet.</p>
  }

  const visible = events.slice(0, visibleCount)

  return (
    <div className="timeline">
      <div className="timeline__controls">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            setVisibleCount(0)
            setPlaying(true)
          }}
        >
          Replay
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={visibleCount >= events.length}
          onClick={() => setPlaying((p) => !p)}
        >
          {playing ? 'Pause' : 'Play'}
        </Button>
        <div className="timeline__speeds">
          {SPEEDS.map((s) => (
            <Button
              key={s}
              variant={speed === s ? 'primary' : 'ghost'}
              size="sm"
              onClick={() => setSpeed(s)}
            >
              {s}x
            </Button>
          ))}
        </div>
        <span className="timeline__progress">
          {visibleCount}/{events.length}
        </span>
      </div>
      <ol className="timeline__list">
        {visible.map((event) => (
          <li key={event.seq} className="timeline__row">
            <span className="timeline__seq">#{event.seq}</span>
            <Badge tone={EVENT_TONE[event.kind]}>
              {eventKindLabel(event.kind, locale)}
            </Badge>
            <span className="timeline__summary">{eventSummary(event, locale)}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
