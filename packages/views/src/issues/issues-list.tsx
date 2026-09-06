'use client'

import { useIssues } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { STATUS_TONE, issueStatusLabel } from './status'

export interface IssuesListProps {
  client: ApiClient
  workspaceId: string
}

export function IssuesList({ client, workspaceId }: IssuesListProps) {
  const { data, isPending, isError, error } = useIssues(client, workspaceId)
  const { locale } = useTranslation()

  if (isPending) {
    return <p className="issues-list__empty">Loading issues…</p>
  }

  if (isError) {
    return (
      <p className="issues-list__empty">
        Failed to load issues: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const issues = data ?? []
  if (issues.length === 0) {
    return <p className="issues-list__empty">No issues yet.</p>
  }

  return (
    <ul className="issues-list">
      {issues.map((issue) => (
        <li key={issue.id}>
          <Card interactive>
            <div className="issues-list__row">
              <span className="issues-list__title">{issue.title}</span>
              <Badge tone={STATUS_TONE[issue.status]}>
                {issueStatusLabel(issue.status, locale)}
              </Badge>
            </div>
          </Card>
        </li>
      ))}
    </ul>
  )
}
