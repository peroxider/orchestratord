'use client'

import { useMemo } from 'react'
import type { CSSProperties } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge } from '@orchestratord/ui'
import type { BadgeTone } from '@orchestratord/ui'
import { useLocale } from '../i18n'
import { modeCopy, modeStatusLabel } from './mode-copy'

export interface CoordinatorGanttProps {
  events: SessionEvent[]
}

interface TaskBar {
  id: string
  title: string
  start: number
  end: number
  eventCount: number
  status: 'completed' | 'running' | 'failed' | 'unknown'
}

const STATUS_TONE: Record<TaskBar['status'], BadgeTone> = {
  completed: 'good',
  running: 'accent',
  failed: 'bad',
  unknown: 'neutral',
}

function taskKey(event: SessionEvent): string {
  const p = event.payload
  const tid = typeof p.task_id === 'string' ? p.task_id : null
  if (tid) return tid
  return `seq-${event.seq}`
}

function taskTitle(event: SessionEvent, fallback: string): string {
  const p = event.payload
  if (typeof p.title === 'string' && p.title.length > 0) return p.title
  if (typeof p.name === 'string' && p.name.length > 0) return p.name
  return fallback
}

function deriveStatus(events: SessionEvent[]): TaskBar['status'] {
  for (const event of [...events].reverse()) {
    const kind = event.kind
    if (kind === 'error') return 'failed'
    if (kind === 'phase_complete' || kind === 'session_complete' || kind === 'turn_complete') {
      return 'completed'
    }
  }
  return events.length > 0 ? 'running' : 'unknown'
}

export function CoordinatorGantt({ events }: CoordinatorGanttProps) {
  const locale = useLocale()
  const c = modeCopy[locale]
  const { bars, totalStart, totalEnd } = useMemo(() => {
    const map = new Map<string, TaskBar>()
    for (const event of events) {
      const id = taskKey(event)
      if (!map.has(id)) {
        map.set(id, {
          id,
          title: taskTitle(event, id),
          start: event.timestamp,
          end: event.timestamp,
          eventCount: 0,
          status: 'unknown',
        })
      }
      const bar = map.get(id)!
      const p = event.payload
      const startedAt =
        typeof p.started_at === 'number'
          ? p.started_at
          : typeof p.started_at === 'string'
            ? Number.parseFloat(p.started_at)
            : null
      const finishedAt =
        typeof p.finished_at === 'number'
          ? p.finished_at
          : typeof p.finished_at === 'string'
            ? Number.parseFloat(p.finished_at)
            : null
      if (startedAt !== null && !Number.isNaN(startedAt)) {
        bar.start = Math.min(bar.start, startedAt)
      } else {
        bar.start = Math.min(bar.start, event.timestamp)
      }
      if (finishedAt !== null && !Number.isNaN(finishedAt)) {
        bar.end = Math.max(bar.end, finishedAt)
      } else {
        bar.end = Math.max(bar.end, event.timestamp)
      }
      bar.eventCount += 1
    }
    const list: TaskBar[] = Array.from(map.values()).map((bar) => {
      const taskEvents: SessionEvent[] = []
      for (const e of events) {
        if (taskKey(e) === bar.id) taskEvents.push(e)
      }
      bar.status = deriveStatus(taskEvents)
      if (bar.end <= bar.start) bar.end = bar.start + 0.001
      return bar
    })
    list.sort((a, b) => a.start - b.start)
    const first = list[0]
    const tStart = first ? first.start : 0
    const tEnd = list.length > 0 ? Math.max(...list.map((b) => b.end)) : 0
    return { bars: list, totalStart: tStart, totalEnd: tEnd }
  }, [events])

  if (bars.length === 0) {
    return <p className="coordinator-gantt__empty">{c.coordinatorEmpty}</p>
  }

  const totalSpan = Math.max(totalEnd - totalStart, 0.001)

  return (
    <div className="coordinator-gantt" aria-label={c.coordinatorLabel}>
      <div className="coordinator-gantt__header">
        <span className="coordinator-gantt__header-label">{c.task}</span>
        <span className="coordinator-gantt__header-track">{c.timeline}</span>
      </div>
      <ol className="coordinator-gantt__rows">
        {bars.map((bar) => {
          const offsetPct = ((bar.start - totalStart) / totalSpan) * 100
          const widthPct = Math.max(((bar.end - bar.start) / totalSpan) * 100, 1.5)
          const style: CSSProperties = {
            marginLeft: `${offsetPct}%`,
            width: `${widthPct}%`,
          }
          return (
            <li
              key={bar.id}
              className={`coordinator-gantt__row coordinator-gantt__row--${bar.status}`}
              data-task-id={bar.id}
            >
              <span className="coordinator-gantt__row-label">
                <span className="coordinator-gantt__row-title">{bar.title}</span>
                <Badge tone={STATUS_TONE[bar.status] ?? 'neutral'}>{modeStatusLabel(bar.status, locale)}</Badge>
              </span>
              <span className="coordinator-gantt__track">
                <span
                  className="coordinator-gantt__bar"
                  style={style}
                  aria-label={`${bar.title} · ${c.timeline}`}
                />
              </span>
              <span className="coordinator-gantt__row-meta">
                {bar.eventCount} {bar.eventCount === 1 ? c.event : c.events}
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
