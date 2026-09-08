import type { SessionEvent, SessionEventKind } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** §5.2.3 timeline coloring: one tone per normalized event kind. */
export const EVENT_TONE: Record<SessionEventKind, BadgeTone> = {
  text: 'accent',
  text_delta: 'accent',
  tool_call: 'purple',
  tool_result: 'neutral',
  turn_complete: 'good',
  phase_complete: 'accent',
  session_complete: 'good',
  error: 'bad',
  goal_set: 'neutral',
  goal_status: 'neutral',
  goal_continue: 'neutral',
  goal_done: 'good',
  goal_cleared: 'neutral',
  goal_paused: 'warn',
  approval_request: 'warn',
  unknown: 'neutral',
}

export function eventKindLabel(
  kind: SessionEventKind,
  locale: Locale = 'en',
): string {
  return translate(locale, `events.kind.${kind}` as TranslationKey, kind.replace(/_/g, ' '))
}

/** One-line summary of an event's payload for timeline rows. */
export function eventSummary(event: SessionEvent, locale: Locale = 'en'): string {
  const p = event.payload
  switch (event.kind) {
    case 'text':
    case 'text_delta':
      return typeof p.text === 'string' ? p.text : ''
    case 'tool_call':
      return typeof p.name === 'string' ? p.name : ''
    case 'tool_result':
      return typeof p.name === 'string'
        ? p.name
        : translate(locale, 'events.summary.tool_result')
    case 'approval_request':
      return typeof p.tool_name === 'string'
        ? p.tool_name
        : translate(locale, 'events.summary.approval_request')
    case 'error':
      return typeof p.message === 'string'
        ? p.message
        : translate(locale, 'events.summary.error')
    case 'turn_complete':
    case 'phase_complete':
    case 'session_complete':
      return typeof p.reason === 'string' ? p.reason : eventKindLabel(event.kind, locale)
    default:
      return eventKindLabel(event.kind, locale)
  }
}
