'use client'

import { useState } from 'react'
import {
  useAddComment,
  useIssue,
  usePullRequests,
  useSessionsByIssue,
} from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Button, Card, Textarea } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { STATUS_TONE, issueStatusLabel } from './status'
import { prStateLabel, prStateTone } from '../vcs/pr-labels'

export interface IssueDetailProps {
  client: ApiClient
  workspaceId: string
  issueId: string
}

export function IssueDetail({
  client,
  workspaceId,
  issueId,
}: IssueDetailProps) {
  const issue = useIssue(client, workspaceId, issueId)
  const addComment = useAddComment(client, workspaceId, issueId)
  const sessions = useSessionsByIssue(client, issueId)
  const pullRequests = usePullRequests(client, issueId)
  const { locale } = useTranslation()
  const c = issueCopy[locale]
  const [body, setBody] = useState('')

  if (issue.isPending) {
    return <p className="issue-detail__empty">{c.loading}</p>
  }
  if (issue.isError) {
    return (
      <p className="issue-detail__empty">
        {c.failed}: {issue.error?.message ?? c.unknown}
      </p>
    )
  }

  const data = issue.data
  const comments = data.comments ?? []

  return (
    <div className="issue-detail">
      <nav className="detail-breadcrumb" aria-label={c.breadcrumb}><a href="/issues">{c.issues}</a><span>/</span><span aria-current="page">{data.id.slice(0, 8)}</span></nav>
      <header className="issue-detail__header">
        <h2 className="issue-detail__title">{data.title}</h2>
        <Badge tone={STATUS_TONE[data.status] ?? 'neutral'}>
          {issueStatusLabel(data.status, locale)}
        </Badge>
      </header>

      <p className="issue-detail__description">
        {data.description || c.noDescription}
      </p>

      <div className="issue-detail__meta">
        <span className="issue-detail__assignee">
          {data.assignee_type
            ? data.assignee_type === 'member' ? c.localOperator : `${data.assignee_type}:${data.assignee_id}`
            : c.unassigned}
        </span>
        {data.labels.map((label) => (
          <Badge key={label} tone="neutral">
            {label}
          </Badge>
        ))}
      </div>

      {pullRequests.data &&
        pullRequests.data.pull_requests.length > 0 && (
          <section className="issue-detail__pull-requests">
            <h3>{c.pullRequests}</h3>
            <ul className="pull-requests">
              {pullRequests.data.pull_requests.map((pr) => (
                <li key={pr.id} className="pull-request">
                  <span className="pull-request__ref">
                    {pr.repo}#{pr.number}
                  </span>
                  <span className="pull-request__title">{pr.title}</span>
                  <Badge tone={prStateTone(pr.state)}>
                    {prStateLabel(pr.state, locale)}
                  </Badge>
                  {pr.status && (
                    <span className="pull-request__status">{pr.status}</span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

      {sessions.data && sessions.data.length > 0 && (
        <section className="issue-detail__sessions">
          <h3>{c.sessions}</h3>
          <ul className="issue-detail__session-list">
            {sessions.data.map((session) => (
              <li key={session.id}>
                <a href={`/sessions/${session.id}`}>
                  {session.mode} — {session.status}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="issue-detail__comments">
        <h3>{c.comments}</h3>
        <ul className="comments">
          {comments.map((comment) => (
            <li key={comment.id} className="comment">
              <Card>
                <p className="comment__body">{comment.body}</p>
                <div className="comment__meta">
                  {comment.mentions.map((mention) => (
                    <Badge key={mention} tone="purple">
                      @{mention}
                    </Badge>
                  ))}
                  <span className="comment__author">{comment.author_type === 'member' ? c.localOperator : comment.author_type}</span>
                </div>
              </Card>
            </li>
          ))}
        </ul>

        <form
          className="comment-composer"
          onSubmit={(e) => {
            e.preventDefault()
            if (!body.trim()) return
            addComment.mutate({
              body,
            })
            setBody('')
          }}
        >
          <Textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder={c.commentPlaceholder}
          />
          <Button type="submit" disabled={!body.trim() || addComment.isPending}>
            {c.comment}
          </Button>
        </form>
      </section>
    </div>
  )
}

const issueCopy = {
  en: { loading: 'Loading issue…', failed: 'Could not load issue', unknown: 'unknown error', breadcrumb: 'Breadcrumb', issues: 'Issues', noDescription: 'No description.', unassigned: 'Unassigned', localOperator: 'Local operator', pullRequests: 'Pull requests', sessions: 'Sessions', comments: 'Comments', commentPlaceholder: 'Write a comment… (@agent-name to mention)', comment: 'Comment' },
  'zh-CN': { loading: '正在加载任务…', failed: '无法加载任务', unknown: '未知错误', breadcrumb: '面包屑导航', issues: '任务', noDescription: '暂无描述。', unassigned: '未分配', localOperator: '本地操作人', pullRequests: '拉取请求', sessions: '会话', comments: '评论', commentPlaceholder: '写下评论…（使用 @agent-name 提及）', comment: '发表评论' },
  ja: { loading: 'Issue を読み込み中…', failed: 'Issue を読み込めませんでした', unknown: '不明なエラー', breadcrumb: 'パンくず', issues: 'Issue', noDescription: '説明はありません。', unassigned: '未割り当て', localOperator: 'ローカルオペレーター', pullRequests: 'プルリクエスト', sessions: 'セッション', comments: 'コメント', commentPlaceholder: 'コメントを書く…（@agent-name でメンション）', comment: 'コメント' },
} as const
