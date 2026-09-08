'use client'

import type { ComponentType } from 'react'
import {
  useAssignInbox,
  useDismissInbox,
  useInbox,
  useResolveInbox,
  type ApiClient,
  type InboxKind,
} from '@orchestratord/core'
import { ApprovalCard } from './approval-card'
import { ClarificationCard } from './clarification-card'
import { FailureCard } from './failure-card'
import type { InboxKindCardProps } from './inbox-shared'

/** §7.4 — one differentiated card view per inbox kind (§7.5 acceptance). */
const KIND_CARDS: Record<InboxKind, ComponentType<InboxKindCardProps>> = {
  approval_request: ApprovalCard,
  clarification: ClarificationCard,
  failed: FailureCard,
}

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
      {items.map((item) => {
        const KindCard = KIND_CARDS[item.kind]
        return (
          <KindCard
            key={item.id}
            item={item}
            workspaceId={workspaceId}
            currentMemberId={currentMemberId}
            busy={assign.isPending || resolve.isPending || dismiss.isPending}
            onAssign={(assigneeId) =>
              assign.mutate({
                itemId: item.id,
                body: { assignee_type: 'member', assignee_id: assigneeId },
              })
            }
            onResolve={() => resolve.mutate({ itemId: item.id })}
            onDismiss={() => dismiss.mutate({ itemId: item.id })}
          />
        )
      })}
    </ul>
  )
}
