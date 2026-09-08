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
  authorType: string
  authorId: string
}

export function IssueDetail({
  client,
  workspaceId,
  issueId,
  authorType,
  authorId,
}: IssueDetailProps) {
  const issue = useIssue(client, workspaceId, issueId)
  const addComment = useAddComment(client, workspaceId, issueId)
  const sessions = useSessionsByIssue(client, issueId)
  const pullRequests = usePullRequests(client, issueId)
  const { locale } = useTranslation()
  const [body, setBody] = useState('')

  if (issue.isPending) {
    return <p className="issue-detail__empty">Loading issue…</p>
  }
  if (issue.isError) {
    return (
      <p className="issue-detail__empty">
        Failed to load issue: {issue.error?.message ?? 'unknown error'}
      </p>
    )
  }

  const data = issue.data
  const comments = data.comments ?? []

  return (
    <div className="issue-detail">
      <header className="issue-detail__header">
        <h2 className="issue-detail__title">{data.title}</h2>
        <Badge tone={STATUS_TONE[data.status]}>
          {issueStatusLabel(data.status, locale)}
        </Badge>
      </header>

      <p className="issue-detail__description">
        {data.description || 'No description.'}
      </p>

      <div className="issue-detail__meta">
        <span className="issue-detail__assignee">
          {data.assignee_type
            ? `${data.assignee_type}:${data.assignee_id}`
            : 'unassigned'}
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
            <h3>Pull requests</h3>
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
          <h3>Sessions</h3>
          <ul className="issue-detail__session-list">
            {sessions.data.map((session) => (
              <li key={session.id}>
                <a href={`/${workspaceId}/sessions/${session.id}`}>
                  {session.mode} — {session.status}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="issue-detail__comments">
        <h3>Comments</h3>
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
                  <span className="comment__author">{comment.author_type}</span>
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
              author_type: authorType,
              author_id: authorId,
            })
            setBody('')
          }}
        >
          <Textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Write a comment… (@agent-name to mention)"
          />
          <Button type="submit" disabled={!body.trim() || addComment.isPending}>
            Comment
          </Button>
        </form>
      </section>
    </div>
  )
}
