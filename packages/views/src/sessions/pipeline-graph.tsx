'use client'

import { useMemo } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge } from '@orchestratord/ui'
import type { BadgeTone } from '@orchestratord/ui'
import { eventKindLabel, eventSummary } from './event-kind'
import { useTranslation } from '../i18n'
import type { Locale } from '../i18n'

export interface PipelineGraphProps {
  events: SessionEvent[]
}

interface StageBucket {
  id: string
  label: string
  events: SessionEvent[]
  status: 'running' | 'completed' | 'failed' | 'unknown'
}

/** Extract the stage partition key from an event payload. Defensive —
 * pipeline mode always sets ``session.stage_id = "pipeline:<stage>"``
 * (``modes/pipeline.py:339``), but the open-source frontend can't assume
 * the backend populates every payload, so we fall back to ``stage_name``
 * and finally to ``"stage-N"`` derived from event seq ordering.
 */
function stageKey(event: SessionEvent, fallbackIndex: number): string {
  const p = event.payload
  const stageId = typeof p.stage_id === 'string' ? p.stage_id : null
  if (stageId) {
    // Strip the ``pipeline:`` prefix the runner adds so the card label
    // stays short (e.g. ``pipeline:analyzer`` -> ``analyzer``).
    const colon = stageId.indexOf(':')
    return colon >= 0 ? stageId.slice(colon + 1) : stageId
  }
  const stageName = typeof p.stage_name === 'string' ? p.stage_name : null
  if (stageName) return stageName
  return `stage-${fallbackIndex}`
}

function deriveStatus(events: SessionEvent[]): StageBucket['status'] {
  // Walk backwards to the most recent terminal event.
  for (const event of [...events].reverse()) {
    const kind = event.kind
    if (kind === 'error') return 'failed'
    if (kind === 'phase_complete' || kind === 'session_complete') return 'completed'
    if (kind === 'turn_complete') return 'completed'
  }
  return events.length > 0 ? 'running' : 'unknown'
}

const STATUS_TONE: Record<StageBucket['status'], BadgeTone> = {
  running: 'accent',
  completed: 'good',
  failed: 'bad',
  unknown: 'neutral',
}

export function PipelineGraph({ events }: PipelineGraphProps) {
  const { locale } = useTranslation()
  const stages = useMemo<StageBucket[]>(() => {
    const buckets = new Map<string, StageBucket>()
    const fallbackIndex = new Map<string, number>()
    for (const event of events) {
      let key = stageKey(event, buckets.size)
      if (!buckets.has(key)) {
        const counter = fallbackIndex.get(key) ?? 0
        fallbackIndex.set(key, counter + 1)
        const label =
          typeof event.payload.stage_name === 'string'
            ? event.payload.stage_name
            : key
        buckets.set(key, {
          id: key,
          label,
          events: [],
          status: 'running',
        })
      }
      buckets.get(key)!.events.push(event)
    }
    const list = Array.from(buckets.values())
    // Sort by first-event seq so the visual chain matches execution order.
    list.sort((a, b) => {
      const aFirst = a.events[0]
      const bFirst = b.events[0]
      if (!aFirst || !bFirst) return 0
      return aFirst.seq - bFirst.seq
    })
    for (const bucket of list) {
      bucket.status = deriveStatus(bucket.events)
    }
    return list
  }, [events])

  if (stages.length === 0) {
    return <p className="pipeline-graph__empty">No pipeline events yet.</p>
  }

  return (
    <ol className="pipeline-graph" aria-label="Pipeline stages">
      {stages.map((stage, index) => (
        <li key={stage.id} className="pipeline-graph__stage">
          <StageCard stage={stage} locale={locale} />
          {index < stages.length - 1 && (
            <div
              className="pipeline-graph__handoff"
              aria-label="context injected into next stage"
            >
              <span className="pipeline-graph__handoff-arrow">↓</span>
              <span className="pipeline-graph__handoff-label">
                context injected
              </span>
            </div>
          )}
        </li>
      ))}
    </ol>
  )
}

function StageCard({ stage, locale }: { stage: StageBucket; locale: Locale }) {
  // Last meaningful text snippet: walk backwards until we find a
  // text_delta / text event. Used as the card's preview so operators
  // can see what the stage actually produced without opening the
  // full transcript.
  let lastText = ''
  for (const event of [...stage.events].reverse()) {
    if (
      (event.kind === 'text' || event.kind === 'text_delta') &&
      typeof event.payload.text === 'string'
    ) {
      lastText = event.payload.text
      break
    }
  }
  const preview = lastText.length > 240 ? `${lastText.slice(0, 240)}…` : lastText
  const lastEvent = stage.events[stage.events.length - 1]

  return (
    <article className="pipeline-graph__card" data-stage-id={stage.id}>
      <header className="pipeline-graph__card-header">
        <h3 className="pipeline-graph__stage-name">{stage.label}</h3>
        <Badge tone={STATUS_TONE[stage.status] ?? 'neutral'}>{stage.status}</Badge>
      </header>
      <dl className="pipeline-graph__meta">
        <div className="pipeline-graph__meta-row">
          <dt>events</dt>
          <dd>{stage.events.length}</dd>
        </div>
        {lastEvent && (
          <div className="pipeline-graph__meta-row">
            <dt>last</dt>
            <dd>
              <span className="pipeline-graph__kind">
                {eventKindLabel(lastEvent.kind, locale)}
              </span>
              <span className="pipeline-graph__summary">
                {eventSummary(lastEvent, locale)}
              </span>
            </dd>
          </div>
        )}
      </dl>
      {preview && <p className="pipeline-graph__preview">{preview}</p>}
    </article>
  )
}
