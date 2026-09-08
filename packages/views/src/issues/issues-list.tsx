'use client'

import { useState } from 'react'

import { useIssues } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { STATUS_TONE, issueStatusLabel } from './status'

export interface IssuesListProps {
  client: ApiClient
  workspaceId: string
}

export function IssuesList({ client, workspaceId }: IssuesListProps) {
  const [visibleCount, setVisibleCount] = useState(100)
  const { data, isPending, isError, error } = useIssues(client, workspaceId)
  const { locale } = useTranslation()
  const c = {
    en: { loading: 'Loading issues…', failed: 'Failed to load issues', empty: 'No issues yet.', more: 'Load 100 more' },
    'zh-CN': { loading: '正在加载任务…', failed: '无法加载任务', empty: '暂无任务。', more: '再加载 100 项' },
    ja: { loading: 'Issue を読み込み中…', failed: 'Issue を読み込めませんでした', empty: 'Issue はまだありません。', more: 'さらに 100 件読み込む' },
  }[locale]

  if (isPending) {
    return <p className="issues-list__empty">{c.loading}</p>
  }

  if (isError) {
    return (
      <p className="issues-list__empty">
        {c.failed}: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const issues = data ?? []
  if (issues.length === 0) {
    return <p className="issues-list__empty">{c.empty}</p>
  }

  return (
    <><ul className="issues-list">
      {issues.slice(0, visibleCount).map((issue) => (
        <li key={issue.id}>
          <a className="issues-list__link" href={`/issues/${issue.id}`}>
            <Card interactive>
              <div className="issues-list__row">
                <span className="issues-list__title">{issue.title}</span>
                <Badge tone={STATUS_TONE[issue.status] ?? 'neutral'}>
                  {issueStatusLabel(issue.status, locale)}
                </Badge>
              </div>
            </Card>
          </a>
        </li>
      ))}
    </ul>{visibleCount < issues.length && <div className="collection-load-more"><Button variant="secondary" onClick={() => setVisibleCount(count => count + 100)}>{c.more} · {Math.min(visibleCount, issues.length)}/{issues.length}</Button></div>}</>
  )
}
