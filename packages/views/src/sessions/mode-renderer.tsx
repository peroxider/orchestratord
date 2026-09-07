'use client'

import type { SessionEvent } from '@orchestratord/core'
import { EventTimeline } from './event-timeline'
import { PipelineGraph } from './pipeline-graph'
import { DebateCards } from './debate-cards'
import { SwarmTree } from './swarm-tree'
import { CoordinatorGantt } from './coordinator-gantt'

export interface ModeRendererProps {
  /** Session mode string from ``Session.mode`` (``single | pipeline | debate | swarm | coordinator``). */
  mode: string
  /** Chronologically ordered events for the session. */
  events: SessionEvent[]
}

/**
 * §5.4 multi-agent mode dispatcher.
 *
 * ``SessionDetail`` renders the full event stream through this component
 * so each orchestrator mode (``pipeline``, ``debate``, ``swarm``,
 * ``coordinator``) gets a tailored visualization. ``single`` falls
 * through to the flat :class:`EventTimeline` because there's nothing
 * to partition.
 *
 * Every renderer is read-only over ``events`` — the merge / coalesce
 * logic lives in ``BackendRunner`` (§5.4.3), and each renderer is
 * defensive: when the expected partitioning key (``payload.stage_id``
 * etc.) is absent on the events, the renderer degrades to a
 * single-section view rather than crashing.
 */
export function ModeRenderer({ mode, events }: ModeRendererProps) {
  switch (mode) {
    case 'pipeline':
      return <PipelineGraph events={events} />
    case 'debate':
      return <DebateCards events={events} />
    case 'swarm':
      return <SwarmTree events={events} />
    case 'coordinator':
      return <CoordinatorGantt events={events} />
    case 'single':
    default:
      return <EventTimeline events={events} />
  }
}