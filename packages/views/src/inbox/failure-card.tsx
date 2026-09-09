'use client'

import { Button } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { translate } from '../i18n/dictionaries'
import { InboxCardShell } from './inbox-shared'
import type { InboxKindCardProps } from './inbox-shared'

/** §7.4 failure view — triage framing (resolve = handled). */
export function FailureCard({
  item,
  workspaceId,
  busy,
  onResolve,
  onDismiss,
  resolveResource,
}: InboxKindCardProps) {
  const { locale } = useTranslation()
  return (
    <InboxCardShell
      item={item}
      workspaceId={workspaceId}
      resolveResource={resolveResource}
      actions={
        <div className="inbox-card__actions">
          <Button size="sm" variant="primary" disabled={busy} onClick={onResolve}>
            {translate(locale, 'inbox.action.mark_handled', 'Mark handled')}
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
          'inbox.hint.failed',
          'A session run failed and needs attention.',
        )}
      </p>
    </InboxCardShell>
  )
}
