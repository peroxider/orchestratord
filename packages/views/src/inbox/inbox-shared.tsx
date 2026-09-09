'use client'

import type { ReactNode } from 'react'
import type { InboxItem } from '@orchestratord/core'
import type { ResourcePresentation, ResourceRef } from '@orchestratord/app-contracts'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { INBOX_STATUS_TONE, inboxKindLabel, inboxStatusLabel } from './inbox-status'

export interface InboxKindCardProps {
  item: InboxItem
  workspaceId: string
  busy: boolean
  onResolve: () => void
  onDismiss: () => void
  onAnswer?: (answer: string) => void
  resolveResource?: (ref: ResourceRef) => ResourcePresentation
}

export interface InboxCardShellProps {
  item: InboxItem
  workspaceId: string
  /** Kind-specific body (hint line, emphasized context) under the title. */
  children?: ReactNode
  /** Kind-specific action row; hidden once the item is resolved/dismissed. */
  actions?: ReactNode
  resolveResource?: (ref: ResourceRef) => ResourcePresentation
}

/** Shared §5.2.6 card chrome so the three §7.4 kind views stay consistent. */
export function InboxCardShell({
  item,
  workspaceId,
  children,
  actions,
  resolveResource,
}: InboxCardShellProps) {
  const { locale } = useTranslation()
  const terminal = item.status === 'resolved' || item.status === 'dismissed'
  const links: { label: string; href: string }[] = []
  if (item.resource_ref && resolveResource) {
    const presentation = resolveResource(item.resource_ref)
    if (presentation.href) links.push({ label: presentation.label, href: presentation.href })
  }
  if (item.session_id) {
    links.push({
      label: 'session',
      href: `/sessions/${item.session_id}`,
    })
  }

  return (
    <li>
      <Card className="inbox-card">
        <header className="inbox-card__header">
          <Badge tone="purple">{inboxKindLabel(item.kind, locale)}</Badge>
          <Badge tone={INBOX_STATUS_TONE[item.status] ?? 'neutral'}>
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
