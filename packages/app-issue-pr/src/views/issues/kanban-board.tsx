'use client'

import { useState } from 'react'
import type { CSSProperties } from 'react'
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCorners,
  useDroppable,
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
import type { ApiClient } from '@orchestratord/core'
import type { Issue, IssueStatus } from '../../api/types'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useTranslation } from '@orchestratord/views'
import type { Locale } from '@orchestratord/views'
import { useIssues, useMoveIssue } from '../../queries/issues'
import { ISSUE_STATUSES, STATUS_TONE, issueStatusLabel } from './status'

export interface KanbanBoardProps {
  client: ApiClient
  workspaceId: string
}

export function KanbanBoard({ client, workspaceId }: KanbanBoardProps) {
  const { data, isPending, isError, error } = useIssues(client, workspaceId)
  const move = useMoveIssue(client, workspaceId)
  const { locale } = useTranslation()
  const c = {
    en: { loading: 'Loading board…', failed: 'Failed to load board', move: 'Move', change: 'Change status for', more: 'Show more' },
    'zh-CN': { loading: '正在加载看板…', failed: '无法加载看板', move: '移动', change: '更改状态：', more: '显示更多' },
    ja: { loading: 'カンバンを読み込み中…', failed: 'カンバンを読み込めませんでした', move: '移動', change: '状態を変更：', more: 'さらに表示' },
  }[locale]
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
    return <p className="kanban__empty">{c.loading}</p>
  }
  if (isError) {
    return (
      <p className="kanban__empty">
        {c.failed}: {error?.message ?? 'unknown error'}
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
    const overStatus = over.data.current?.status
    const targetStatus = (
      typeof overStatus === 'string'
        ? overStatus
        : String(over.id).replace(/^column:/, '')
    ) as IssueStatus
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
              moveLabel={c.move}
              changeLabel={c.change}
              moreLabel={c.more}
              onMove={(issueId, nextStatus) => move.mutate({ issueId, status: nextStatus })}
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
  onMove,
  moveLabel,
  changeLabel,
  moreLabel,
}: {
  status: IssueStatus
  issues: Issue[]
  workspaceId: string
  activeId: string | null
  locale: Locale
  onMove: (issueId: string, status: IssueStatus) => void
  moveLabel: string
  changeLabel: string
  moreLabel: string
}) {
  const [visibleCount, setVisibleCount] = useState(50)
  const visibleIssues = issues.slice(0, visibleCount)
  // Each column is a droppable region. The droppable id is the column
  // status so ``handleDragEnd`` can read the target column from
  // ``event.over.id`` without scanning the DOM.
  const { setNodeRef, isOver } = useDroppable({
    id: `column:${status}`,
    data: { status },
  })
  return (
    <SortableContext
      id={status}
      items={visibleIssues.map((i) => i.id)}
      strategy={verticalListSortingStrategy}
    >
      <section
        ref={setNodeRef}
        className="kanban-column"
        data-status={status}
        data-over={isOver || undefined}
        aria-label={`Column ${issueStatusLabel(status, locale)}`}
      >
        <header className="kanban-column__header">
          <Badge tone={STATUS_TONE[status] ?? 'neutral'}>
            {issueStatusLabel(status, locale)}
          </Badge>
          <span className="kanban-column__count">{issues.length}</span>
        </header>
        <div className="kanban-column__body">
          {visibleIssues.map((issue) => (
            <KanbanCard
              key={issue.id}
              issue={issue}
              workspaceId={workspaceId}
              active={activeId === issue.id}
              locale={locale}
              onMove={onMove}
              moveLabel={moveLabel}
              changeLabel={changeLabel}
            />
          ))}
          {visibleCount < issues.length && <Button size="sm" variant="ghost" onClick={() => setVisibleCount(count => count + 50)}>{moreLabel} · {visibleIssues.length}/{issues.length}</Button>}
        </div>
      </section>
    </SortableContext>
  )
}

function KanbanCard({
  issue,
  workspaceId,
  active,
  locale,
  onMove,
  moveLabel,
  changeLabel,
}: {
  issue: Issue
  workspaceId: string
  active: boolean
  locale: Locale
  onMove: (issueId: string, status: IssueStatus) => void
  moveLabel: string
  changeLabel: string
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: issue.id, data: { status: issue.status } })

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
        <label className="kanban-card__move">
          <span>{moveLabel}</span>
          <select
            aria-label={`${changeLabel} ${issue.title}`}
            value={issue.status}
            onPointerDown={(event) => event.stopPropagation()}
            onChange={(event) => onMove(issue.id, event.target.value as IssueStatus)}
          >
            {ISSUE_STATUSES.map((status) => (
              <option key={status} value={status}>{issueStatusLabel(status, locale)}</option>
            ))}
          </select>
        </label>
      </Card>
    </div>
  )
}
