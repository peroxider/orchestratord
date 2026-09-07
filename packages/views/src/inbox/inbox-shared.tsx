'use client'

import type { ReactNode } from 'react'
import type { InboxItem } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { INBOX_STATUS_TONE, inboxKindLabel, inboxStatusLabel } from './inbox-status'

export interface InboxKindCardProps {
  item: InboxItem
  workspaceId: string
  /** The signed-in member id used by "Assign to me" (dev stub until §5.7.4). */
  currentMemberId?: string
  busy: boolean
  onAssign: (assigneeId: string) => void
  onResolve: () => void
  onDismiss: () => void
}

export interface InboxCardShellProps {
  item: InboxItem
  workspaceId: string
  /** Kind-specific body (hint line, emphasized context) under the title. */
  children?: ReactNode
  /** Kind-specific action row; hidden once the item is resolved/dismissed. */
  actions?: ReactNode
}

/** Shared §5.2.6 card chrome so the three §7.4 kind views stay consistent. */
export function InboxCardShell({
  item,
  workspaceId,
  children,
  actions,
}: InboxCardShellProps) {
  const { locale } = useTranslation()
  const terminal = item.status === 'resolved' || item.status === 'dismissed'
  const links: { label: string; href: string }[] = []
  if (item.issue_id) {
    links.push({ label: 'issue', href: `/${workspaceId}/issues/${item.issue_id}` })
  }
  if (item.session_id) {
    links.push({
      label: 'session',
      href: `/${workspaceId}/sessions/${item.session_id}`,
    })
  }

  return (
    <li>
      <Card className="inbox-card">
        <header className="inbox-card__header">
          <Badge tone="purple">{inboxKindLabel(item.kind, locale)}</Badge>
          <Badge tone={INBOX_STATUS_TONE[item.status]}>
            {inboxStatusLabel(item.status, locale)}
          </Badge>
        </header>
        <p className="inbox-card__title">{item.title}</p>
        {children}
        {links.length > 0 && (
          <div className="inbox-card__links">
            {links.map((l) => (
              <a key={l.label} href={l.href}>
                {l.label}
              </a>
            ))}
          </div>
        )}
        {!terminal && actions}
      </Card>
    </li>
  )
}
