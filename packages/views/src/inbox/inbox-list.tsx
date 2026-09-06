'use client'

import {
  useAssignInbox,
  useDismissInbox,
  useInbox,
  useResolveInbox,
  type ApiClient,
  type InboxItem,
} from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import {
  INBOX_STATUS_TONE,
  inboxKindLabel,
  inboxStatusLabel,
} from './inbox-status'

export interface InboxListProps {
  client: ApiClient
  workspaceId: string
  /** The signed-in member id used by "Assign to me" (dev stub until §5.7.4). */
  currentMemberId?: string
}

export function InboxList({ client, workspaceId, currentMemberId }: InboxListProps) {
  const { data, isPending, isError, error } = useInbox(client, workspaceId)
  const assign = useAssignInbox(client, workspaceId)
  const resolve = useResolveInbox(client, workspaceId)
  const dismiss = useDismissInbox(client, workspaceId)

  if (isPending) {
    return <p className="inbox__empty">Loading inbox…</p>
  }
  if (isError) {
    return (
      <p className="inbox__empty">
        Failed to load inbox: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const items = data ?? []
  if (items.length === 0) {
    return <p className="inbox__empty">No items needing attention.</p>
  }

  return (
    <ul className="inbox">
      {items.map((item) => (
        <InboxCard
          key={item.id}
          item={item}
          workspaceId={workspaceId}
          currentMemberId={currentMemberId}
          onAssign={(assigneeId) =>
            assign.mutate({
              itemId: item.id,
              body: { assignee_type: 'member', assignee_id: assigneeId },
            })
          }
          onResolve={() => resolve.mutate({ itemId: item.id })}
          onDismiss={() => dismiss.mutate({ itemId: item.id })}
          busy={assign.isPending || resolve.isPending || dismiss.isPending}
        />
      ))}
    </ul>
  )
}

function InboxCard({
  item,
  workspaceId,
  currentMemberId,
  onAssign,
  onResolve,
  onDismiss,
  busy,
}: {
  item: InboxItem
  workspaceId: string
  currentMemberId?: string
  onAssign: (assigneeId: string) => void
  onResolve: () => void
  onDismiss: () => void
  busy: boolean
}) {
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
        {links.length > 0 && (
          <div className="inbox-card__links">
            {links.map((l) => (
              <a key={l.label} href={l.href}>
                {l.label}
              </a>
            ))}
          </div>
        )}
        {!terminal && (
          <div className="inbox-card__actions">
            {item.status === 'open' && currentMemberId && (
              <Button
                size="sm"
                variant="secondary"
                disabled={busy}
                onClick={() => onAssign(currentMemberId)}
              >
                Assign to me
              </Button>
            )}
            <Button size="sm" variant="primary" disabled={busy} onClick={onResolve}>
              Resolve
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={onDismiss}>
              Dismiss
            </Button>
          </div>
        )}
      </Card>
    </li>
  )
}
