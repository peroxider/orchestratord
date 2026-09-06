'use client'

import { useState } from 'react'
import { useIssues, useMoveIssue } from '@orchestratord/core'
import type { ApiClient, Issue, IssueStatus } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { ISSUE_STATUSES, STATUS_TONE, issueStatusLabel } from './status'

export interface KanbanBoardProps {
  client: ApiClient
  workspaceId: string
}

export function KanbanBoard({ client, workspaceId }: KanbanBoardProps) {
  const { data, isPending, isError, error } = useIssues(client, workspaceId)
  const move = useMoveIssue(client, workspaceId)
  const { locale } = useTranslation()
  const [draggingId, setDraggingId] = useState<string | null>(null)

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

  return (
    <div className="kanban">
      {columns.map((status) => {
        const columnIssues = issues.filter((i) => i.status === status)
        return (
          <section
            key={status}
            className="kanban-column"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              const issueId = e.dataTransfer.getData('text/plain')
              if (issueId) {
                move.mutate({ issueId, status })
              }
              setDraggingId(null)
            }}
          >
            <header className="kanban-column__header">
              <Badge tone={STATUS_TONE[status]}>
                {issueStatusLabel(status, locale)}
              </Badge>
              <span className="kanban-column__count">{columnIssues.length}</span>
            </header>
            <div className="kanban-column__body">
              {columnIssues.map((issue) => (
                <KanbanCard
                  key={issue.id}
                  issue={issue}
                  workspaceId={workspaceId}
                  dragging={draggingId === issue.id}
                  onDragStart={() => setDraggingId(issue.id)}
                  onDragEnd={() => setDraggingId(null)}
                />
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function KanbanCard({
  issue,
  workspaceId,
  dragging,
  onDragStart,
  onDragEnd,
}: {
  issue: Issue
  workspaceId: string
  dragging: boolean
  onDragStart: () => void
  onDragEnd: () => void
}) {
  return (
    <Card
      interactive
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('text/plain', issue.id)
        e.dataTransfer.effectAllowed = 'move'
        onDragStart()
      }}
      onDragEnd={onDragEnd}
      className={dragging ? 'kanban-card kanban-card--dragging' : 'kanban-card'}
    >
      <a
        className="kanban-card__link"
        href={`/${workspaceId}/issues/${issue.id}`}
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
  )
}
