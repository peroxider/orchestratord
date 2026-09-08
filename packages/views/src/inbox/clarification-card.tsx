'use client'

import { Button } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { translate } from '../i18n/dictionaries'
import { InboxCardShell } from './inbox-shared'
import type { InboxKindCardProps } from './inbox-shared'

/** §7.4 clarification view — answer-first framing (resolve = answered). */
export function ClarificationCard({
  item,
  workspaceId,
  busy,
  onResolve,
  onDismiss,
}: InboxKindCardProps) {
  const { locale } = useTranslation()
  return (
    <InboxCardShell
      item={item}
      workspaceId={workspaceId}
      actions={
        <div className="inbox-card__actions">
          <Button size="sm" variant="primary" disabled={busy} onClick={onResolve}>
            {translate(locale, 'inbox.action.mark_answered', 'Mark answered')}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={onDismiss}>
            {translate(locale, 'inbox.action.dismiss', 'Dismiss')}
          </Button>
        </div>
      }
    >
      <p className="inbox-card__hint">
        {translate(
          locale,
          'inbox.hint.clarification',
          'The agent asked a clarifying question.',
        )}
      </p>
    </InboxCardShell>
  )
}
