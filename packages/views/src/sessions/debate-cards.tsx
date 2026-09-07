'use client'

import { useMemo } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge } from '@orchestratord/ui'
import type { BadgeTone } from '@orchestratord/ui'
import { eventKindLabel, eventSummary } from './event-kind'
import { useTranslation } from '../i18n'
import type { Locale } from '../i18n'

export interface DebateCardsProps {
  events: SessionEvent[]
}

interface ParticipantBucket {
  id: string
  label: string
  lens: string | null
  events: SessionEvent[]
  status: 'running' | 'completed' | 'failed' | 'unknown'
}

function participantKey(event: SessionEvent): string {
  const p = event.payload
  const stageName = typeof p.stage_name === 'string' ? p.stage_name : null
  if (stageName) return stageName
  const proposerId = typeof p.proposer_id === 'string' ? p.proposer_id : null
  if (proposerId) return proposerId
  return 'discussion'
}

function deriveStatus(events: SessionEvent[]): ParticipantBucket['status'] {
  for (const event of [...events].reverse()) {
    const kind = event.kind
    if (kind === 'error') return 'failed'
    if (kind === 'phase_complete' || kind === 'session_complete') return 'completed'
    if (kind === 'turn_complete') return 'completed'
  }
  return events.length > 0 ? 'running' : 'unknown'
}

const STATUS_TONE: Record<ParticipantBucket['status'], BadgeTone> = {
  running: 'accent',
  completed: 'good',
  failed: 'bad',
  unknown: 'neutral',
}

export function DebateCards({ events }: DebateCardsProps) {
  const { locale } = useTranslation()
  const buckets = useMemo<ParticipantBucket[]>(() => {
    const map = new Map<string, ParticipantBucket>()
    for (const event of events) {
      const key = participantKey(event)
      if (!map.has(key)) {
        map.set(key, {
          id: key,
          label: key,
          lens:
            typeof event.payload.lens === 'string' ? event.payload.lens : null,
          events: [],
          status: 'running',
        })
      }
      map.get(key)!.events.push(event)
    }
    const list = Array.from(map.values())
    list.sort((a, b) => {
      if (a.id === 'judge') return 1
      if (b.id === 'judge') return -1
      return a.id.localeCompare(b.id)
    })
    for (const bucket of list) {
      bucket.status = deriveStatus(bucket.events)
    }
    return list
  }, [events])

  const proposers = buckets.filter((b) => b.id !== 'judge')
  const judge = buckets.find((b) => b.id === 'judge')

  if (buckets.length === 0) {
    return <p className="debate-cards__empty">No debate events yet.</p>
  }

  return (
    <div className="debate-cards" aria-label="Debate participants">
      <div className="debate-cards__proposer-row">
        {proposers.length === 0 && (
          <p className="debate-cards__empty">
            No proposer events yet — judge card will appear when judging starts.
          </p>
        )}
        {proposers.map((proposer) => (
          <ProposerCard
            key={proposer.id}
            bucket={proposer}
            locale={locale}
          />
        ))}
      </div>
      <div className="debate-cards__badge-row">
        <Badge tone="accent" className="debate-cards__independence-badge">
          Independent reasoning — proposers did not see each other
        </Badge>
      </div>
      {judge && <JudgeCard bucket={judge} locale={locale} />}
      {!judge && (
        <p className="debate-cards__pending-judge">
          Judge pending — runs after both proposers complete.
        </p>
      )}
    </div>
  )
}

function ProposerCard({
  bucket,
  locale,
}: {
  bucket: ParticipantBucket
  locale: Locale
}) {
  const preview = lastTextSnippet(bucket.events)
  const lastEvent = bucket.events[bucket.events.length - 1]

  return (
    <article className="debate-cards__card" data-participant-id={bucket.id}>
      <header className="debate-cards__card-header">
        <h3 className="debate-cards__name">{bucket.label}</h3>
        <Badge tone={STATUS_TONE[bucket.status]}>{bucket.status}</Badge>
      </header>
      {bucket.lens && (
        <p className="debate-cards__lens">
          <span className="debate-cards__lens-label">Lens</span>
          <span className="debate-cards__lens-value">{bucket.lens}</span>
        </p>
      )}
      <dl className="debate-cards__meta">
        <div>
          <dt>events</dt>
          <dd>{bucket.events.length}</dd>
        </div>
        {lastEvent && (
          <div>
            <dt>last</dt>
            <dd>
              <span>{eventKindLabel(lastEvent.kind, locale)}</span>
              <span>{eventSummary(lastEvent, locale)}</span>
            </dd>
          </div>
        )}
      </dl>
      {preview && <p className="debate-cards__preview">{preview}</p>}
    </article>
  )
}

function JudgeCard({
  bucket,
  locale,
}: {
  bucket: ParticipantBucket
  locale: Locale
}) {
  const preview = lastTextSnippet(bucket.events)
  const lastEvent = bucket.events[bucket.events.length - 1]

  return (
    <article
      className="debate-cards__card debate-cards__card--judge"
      data-participant-id={bucket.id}
    >
      <header className="debate-cards__card-header">
        <h3 className="debate-cards__name">Judge</h3>
        <Badge tone={STATUS_TONE[bucket.status]}>{bucket.status}</Badge>
      </header>
      <p className="debate-cards__judge-note">
        Saw both proposers' outputs verbatim and implemented the winner.
      </p>
      <dl className="debate-cards__meta">
        <div>
          <dt>events</dt>
          <dd>{bucket.events.length}</dd>
        </div>
        {lastEvent && (
          <div>
            <dt>last</dt>
            <dd>
              <span>{eventKindLabel(lastEvent.kind, locale)}</span>
              <span>{eventSummary(lastEvent, locale)}</span>
            </dd>
          </div>
        )}
      </dl>
      {preview && <p className="debate-cards__preview">{preview}</p>}
    </article>
  )
}

function lastTextSnippet(events: SessionEvent[]): string {
  for (const event of [...events].reverse()) {
    if (
      (event.kind === 'text' || event.kind === 'text_delta') &&
      typeof event.payload.text === 'string'
    ) {
      const text = event.payload.text
      return text.length > 240 ? `${text.slice(0, 240)}…` : text
    }
  }
  return ''
}