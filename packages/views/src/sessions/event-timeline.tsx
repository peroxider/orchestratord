'use client'

import { useEffect, useState } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { EVENT_TONE, eventKindLabel, eventSummary } from './event-kind'
import { ToolCallCard } from './tool-call-card'
import { ToolResultCard } from './tool-result-card'

export interface EventTimelineProps {
  events: SessionEvent[]
}

const SPEEDS = [0.5, 1, 2] as const

export function EventTimeline({ events }: EventTimelineProps) {
  const [visibleCount, setVisibleCount] = useState(events.length)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<number>(1)
  const { locale } = useTranslation()
  const c = timelineCopy[locale]

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
    return <p className="timeline__empty">{c.empty}</p>
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
          {c.replay}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={visibleCount >= events.length}
          onClick={() => setPlaying((p) => !p)}
        >
          {playing ? c.pause : c.play}
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
          <li key={event.seq} className={`timeline__row${event.kind === 'tool_call' || event.kind === 'tool_result' ? ' timeline__row--evidence' : ''}`}>
            {event.kind === 'tool_call' ? <><span className="timeline__seq">#{event.seq}</span><ToolCallCard event={event} /></> : event.kind === 'tool_result' ? <><span className="timeline__seq">#{event.seq}</span><ToolResultCard event={event} /></> : <><span className="timeline__seq">#{event.seq}</span><Badge tone={EVENT_TONE[event.kind] ?? 'neutral'}>{eventKindLabel(event.kind, locale)}</Badge><span className="timeline__summary">{eventSummary(event, locale)}</span></>}
          </li>
        ))}
      </ol>
    </div>
  )
}

const timelineCopy = {
  en: { empty: 'No events yet.', replay: 'Replay', pause: 'Pause', play: 'Play' },
  'zh-CN': { empty: '暂无事件。', replay: '重放', pause: '暂停', play: '播放' },
  ja: { empty: 'イベントはまだありません。', replay: 'リプレイ', pause: '一時停止', play: '再生' },
} as const
