'use client'

import { useEffect, useRef } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'
import { eventKindLabel, eventSummary } from '../../sessions/event-kind'
import { ToolCallCard } from '../../sessions/tool-call-card'
import { ToolResultCard } from '../../sessions/tool-result-card'
import { useTranslation } from '../../i18n'
import type { Locale } from '../../i18n'

export interface TranscriptDialogProps {
  open: boolean
  onClose: () => void
  events: SessionEvent[]
}

/** §5.5 transcript overlay — opens from the session list / detail page
 * and renders the full event timeline. ``tool_call`` and ``tool_result``
 * events are routed through their dedicated foldable cards
 * (4 KB truncation honored) so 100 KB stdout blobs don't crash the
 * dialog. Everything else renders as a flat row using the same
 * event-summary helper that the timeline uses, so the overlay is
 * visually consistent with the on-page timeline.
 */
export function TranscriptDialog({ open, onClose, events }: TranscriptDialogProps) {
  const ref = useRef<HTMLDialogElement | null>(null)
  const { locale } = useTranslation()
  const typedLocale: Locale = locale

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) {
      dialog.showModal()
    } else if (!open && dialog.open) {
      dialog.close()
    }
  }, [open])

  // Native ``<dialog>`` fires ``cancel`` on ESC and ``close`` on form
  // submission / programmatic close. Forward both to the consumer so
  // React state stays the source of truth.
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    const handleClose = () => onClose()
    dialog.addEventListener('close', handleClose)
    dialog.addEventListener('cancel', handleClose)
    return () => {
      dialog.removeEventListener('close', handleClose)
      dialog.removeEventListener('cancel', handleClose)
    }
  }, [onClose])

  return (
    <dialog ref={ref} className="transcript-dialog" aria-label="Session transcript">
      <header className="transcript-dialog__header">
        <h2 className="transcript-dialog__title">Transcript</h2>
        <span className="transcript-dialog__meta">
          {events.length} event{events.length === 1 ? '' : 's'}
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          aria-label="Close transcript"
        >
          close
        </Button>
      </header>
      <ol className="transcript-dialog__list">
        {events.map((event) => (
          <li
            key={event.seq}
            className="transcript-dialog__row"
            data-event-kind={event.kind}
          >
            {event.kind === 'tool_call' ? (
              <ToolCallCard event={event} />
            ) : event.kind === 'tool_result' ? (
              <ToolResultCard event={event} />
            ) : (
              <div className="transcript-dialog__flat-row">
                <span className="transcript-dialog__seq">#{event.seq}</span>
                <Badge>{eventKindLabel(event.kind, typedLocale)}</Badge>
                <span className="transcript-dialog__summary">
                  {eventSummary(event, typedLocale)}
                </span>
              </div>
            )}
          </li>
        ))}
      </ol>
    </dialog>
  )
}