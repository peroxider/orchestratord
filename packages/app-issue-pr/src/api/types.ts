export type IssueStatus = 'queued' | 'pending' | 'running' | 'pending_review' | 'completed' | 'failed' | 'abandoned' | 'verification_failed'

export interface Issue {
  id: string
  workspace_id: string
  title: string
  description: string
  status: IssueStatus
  assignee_type: string | null
  assignee_id: string | null
  labels: string[]
  created_at: string
  comments?: IssueComment[]
}

export interface IssueComment {
  id: string
  issue_id: string
  author_type: string
  author_id: string
  body: string
  mentions: string[]
  created_at: string
}

export interface PullRequest {
  id: string
  issue_id: string | null
  repo: string
  number: number
  title: string
  state: string
  head_sha: string
  status: string
  created_at: string
  updated_at: string
}

export interface PullRequestsResponse { pull_requests: PullRequest[] }
