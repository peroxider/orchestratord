'use client'

import { Button } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { translate } from '../i18n/dictionaries'
import { InboxCardShell } from './inbox-shared'
import type { InboxKindCardProps } from './inbox-shared'

/** §7.4 approval_request view — the decision pair (approve = resolve). */
export function ApprovalCard({
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
            {translate(locale, 'inbox.action.approve', 'Approve')}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={onDismiss}>
            {translate(locale, 'inbox.action.reject', 'Reject')}
          </Button>
        </div>
      }
    >
      <p className="inbox-card__hint">
        {translate(
          locale,
          'inbox.hint.approval_request',
          'The agent is waiting for approval before it can continue.',
        )}
      </p>
    </InboxCardShell>
  )
}
