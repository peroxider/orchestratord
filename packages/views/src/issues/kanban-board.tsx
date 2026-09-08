'use client'

import { useState } from 'react'
import type { CSSProperties } from 'react'
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCorners,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useIssues, useMoveIssue } from '@orchestratord/core'
import type { ApiClient, Issue, IssueStatus } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import type { Locale } from '../i18n/dictionaries'
import { ISSUE_STATUSES, STATUS_TONE, issueStatusLabel } from './status'

export interface KanbanBoardProps {
  client: ApiClient
  workspaceId: string
}

export function KanbanBoard({ client, workspaceId }: KanbanBoardProps) {
  const { data, isPending, isError, error } = useIssues(client, workspaceId)
  const move = useMoveIssue(client, workspaceId)
  const { locale } = useTranslation()
  // Optimistic update + WS rollback are owned by ``useMoveIssue``
  // (§5.3): the mutation's ``onMutate`` snaps the card into the target
  // column via setQueryData before the PATCH resolves, and ``onError``
  // restores the snapshot if the server rejects the move. A concurrent
  // WS ``issue.*`` event (§5.4.3) also lands in the same cache, so
  // the optimistic move and the authoritative move share one source
  // of truth.
  const [activeId, setActiveId] = useState<string | null>(null)
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  )

  if (isPending) {
    return <p className="kanban__empty">Loading board…</p>
  }
  if (isError) {
    return (
      <p className="kanban__empty">
        Failed to load board: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const issues = data ?? []
  const extraStatuses = Array.from(new Set(issues.map((i) => i.status))).filter(
    (s) => !ISSUE_STATUSES.includes(s),
  )
  const columns: IssueStatus[] = [...ISSUE_STATUSES, ...extraStatuses]

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event
    setActiveId(null)
    if (!over) return
    const issueId = String(active.id)
    const targetStatus = String(over.id) as IssueStatus
    const issue = issues.find((i) => i.id === issueId)
    if (!issue || issue.status === targetStatus) return
    move.mutate({ issueId, status: targetStatus })
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCorners}
      onDragStart={(e) => setActiveId(String(e.active.id))}
      onDragCancel={() => setActiveId(null)}
      onDragEnd={handleDragEnd}
    >
      <div className="kanban">
        {columns.map((status) => {
          const columnIssues = issues.filter((i) => i.status === status)
          return (
            <KanbanColumn
              key={status}
              status={status}
              issues={columnIssues}
              workspaceId={workspaceId}
              activeId={activeId}
              locale={locale}
            />
          )
        })}
      </div>
    </DndContext>
  )
}

function KanbanColumn({
  status,
  issues,
  workspaceId,
  activeId,
  locale,
}: {
  status: IssueStatus
  issues: Issue[]
  workspaceId: string
  activeId: string | null
  locale: Locale
}) {
  // Each column is a droppable region. The droppable id is the column
  // status so ``handleDragEnd`` can read the target column from
  // ``event.over.id`` without scanning the DOM.
  return (
    <SortableContext
      id={status}
      items={issues.map((i) => i.id)}
      strategy={verticalListSortingStrategy}
    >
      <section
        className="kanban-column"
        data-status={status}
        aria-label={`Column ${issueStatusLabel(status, locale)}`}
      >
        <header className="kanban-column__header">
          <Badge tone={STATUS_TONE[status]}>
            {issueStatusLabel(status, locale)}
          </Badge>
          <span className="kanban-column__count">{issues.length}</span>
        </header>
        <div className="kanban-column__body">
          {issues.map((issue) => (
            <KanbanCard
              key={issue.id}
              issue={issue}
              workspaceId={workspaceId}
              active={activeId === issue.id}
            />
          ))}
        </div>
      </section>
    </SortableContext>
  )
}

function KanbanCard({
  issue,
  workspaceId,
  active,
}: {
  issue: Issue
  workspaceId: string
  active: boolean
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: issue.id })

  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  }

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      data-issue-id={issue.id}
    >
      <Card
        interactive
        className={
          active ? 'kanban-card kanban-card--dragging' : 'kanban-card'
        }
      >
        <a
          className="kanban-card__link"
          href={`/issues/${issue.id}`}
          draggable={false}
          onDragStart={(e) => e.preventDefault()}
        >
          {issue.title}
        </a>
        {issue.labels.length > 0 && (
          <div className="kanban-card__labels">
            {issue.labels.map((label) => (
              <Badge key={label} tone="neutral">
                {label}
              </Badge>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
