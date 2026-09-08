'use client'

import { useMemo } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge } from '@orchestratord/ui'
import type { BadgeTone } from '@orchestratord/ui'
import { useLocale, type Locale } from '../i18n'
import { modeCopy, modeStatusLabel } from './mode-copy'

export interface SwarmTreeProps {
  events: SessionEvent[]
}

type TaskStatus = 'completed' | 'in_progress' | 'todo' | 'pending' | 'failed'

interface SubtaskNode {
  id: string
  title: string
  status: TaskStatus
  wave: number
  events: SessionEvent[]
}

interface WaveNode {
  index: number
  subtasks: SubtaskNode[]
}

const STATUS_TONE: Record<TaskStatus, BadgeTone> = {
  completed: 'good',
  in_progress: 'accent',
  todo: 'neutral',
  pending: 'warn',
  failed: 'bad',
}

function normalizeStatus(value: unknown): TaskStatus {
  if (typeof value !== 'string') return 'pending'
  const v = value.toLowerCase()
  if (v === 'completed' || v === 'complete' || v === 'done') return 'completed'
  if (v === 'in_progress' || v === 'in-progress' || v === 'running') return 'in_progress'
  if (v === 'todo' || v === 'pending_todo') return 'todo'
  if (v === 'failed' || v === 'error') return 'failed'
  return 'pending'
}

function taskKey(event: SessionEvent): string {
  const p = event.payload
  const tid = typeof p.task_id === 'string' ? p.task_id : null
  if (tid) return tid
  const sid = typeof p.subtask_id === 'string' ? p.subtask_id : null
  if (sid) return sid
  return `seq-${event.seq}`
}

function waveOf(event: SessionEvent): number {
  const p = event.payload
  const w = typeof p.wave_index === 'number' ? p.wave_index : null
  if (w !== null) return w
  const wStr = typeof p.wave_index === 'string' ? Number.parseInt(p.wave_index, 10) : NaN
  if (!Number.isNaN(wStr)) return wStr
  return 0
}

export function SwarmTree({ events }: SwarmTreeProps) {
  const locale = useLocale()
  const c = modeCopy[locale]
  const waves = useMemo<WaveNode[]>(() => {
    const byId = new Map<string, SubtaskNode>()
    for (const event of events) {
      const id = taskKey(event)
      if (!byId.has(id)) {
        byId.set(id, {
          id,
          title:
            typeof event.payload.title === 'string'
              ? event.payload.title
              : id,
          status: 'pending',
          wave: waveOf(event),
          events: [],
        })
      }
      byId.get(id)!.events.push(event)
    }
    for (const subtask of byId.values()) {
      let derived: TaskStatus | null = null
      for (const e of [...subtask.events].reverse()) {
        if (e.kind === 'phase_complete' || e.kind === 'session_complete' || e.kind === 'turn_complete') {
          derived = 'completed'
          break
        }
        if (e.kind === 'error') {
          derived = 'failed'
          break
        }
      }
      const lastEvent = subtask.events[subtask.events.length - 1]
      const explicit = lastEvent ? normalizeStatus(lastEvent.payload.status) : null
      if (explicit && explicit !== 'pending') {
        subtask.status = explicit
      } else if (derived) {
        subtask.status = derived
      }
    }
    const waveMap = new Map<number, SubtaskNode[]>()
    for (const subtask of byId.values()) {
      const list = waveMap.get(subtask.wave) ?? []
      list.push(subtask)
      waveMap.set(subtask.wave, list)
    }
    const list: WaveNode[] = Array.from(waveMap.entries())
      .sort(([a], [b]) => a - b)
      .map(([index, subtasks]) => ({
        index,
        subtasks: subtasks.sort((a, b) => a.id.localeCompare(b.id)),
      }))
    return list
  }, [events])

  if (waves.length === 0) {
    return <p className="swarm-tree__empty">{c.swarmEmpty}</p>
  }

  return (
    <ol className="swarm-tree" aria-label={c.swarmLabel}>
      {waves.map((wave) => (
        <li key={wave.index} className="swarm-tree__wave" data-wave-index={wave.index}>
          <header className="swarm-tree__wave-header">
            <h3 className="swarm-tree__wave-title">{c.wave} {wave.index + 1}</h3>
            <WaveSummary subtasks={wave.subtasks} locale={locale} />
          </header>
          <ul className="swarm-tree__subtasks">
            {wave.subtasks.map((subtask) => (
              <li
                key={subtask.id}
                className={`swarm-tree__subtask swarm-tree__subtask--${subtask.status}`}
                data-task-id={subtask.id}
              >
                <div className="swarm-tree__subtask-row">
                  <span className="swarm-tree__subtask-title">{subtask.title}</span>
                  <Badge tone={STATUS_TONE[subtask.status] ?? 'neutral'}>{modeStatusLabel(subtask.status, locale)}</Badge>
                </div>
                <span className="swarm-tree__subtask-meta">
                  {subtask.events.length} {subtask.events.length === 1 ? c.event : c.events}
                </span>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ol>
  )
}

function WaveSummary({ subtasks, locale }: { subtasks: SubtaskNode[]; locale: Locale }) {
  const c = modeCopy[locale]
  const counts: Record<TaskStatus, number> = {
    completed: 0,
    in_progress: 0,
    todo: 0,
    pending: 0,
    failed: 0,
  }
  for (const s of subtasks) {
    counts[s.status] += 1
  }
  return (
    <span className="swarm-tree__wave-summary">
      <Badge tone="good">{counts.completed} {c.done}</Badge>
      <Badge tone="accent">{counts.in_progress} {c.running}</Badge>
      <Badge tone="neutral">{counts.todo + counts.pending} {c.pending}</Badge>
      {counts.failed > 0 && <Badge tone="bad">{counts.failed} {c.failed}</Badge>}
    </span>
  )
}
