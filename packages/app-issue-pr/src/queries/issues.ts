import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { adaptSession, type ApiClient, type Session } from '@orchestratord/core'
import type { Issue, IssueComment } from '../api/types'

export interface IssueFilters {
  status?: string
  assignee_id?: string
  label?: string
}

export function toQueryString(filters: IssueFilters): string {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.assignee_id) params.set('assignee_id', filters.assignee_id)
  if (filters.label) params.set('label', filters.label)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function useIssues(
  client: ApiClient,
  workspaceId: string,
  filters?: IssueFilters,
) {
  return useQuery({
    queryKey: ['application', 'issue_pr', 'issues', workspaceId, filters],
    queryFn: () =>
      client.request<Issue[]>(
        `/api/workspaces/${workspaceId}/issues${toQueryString(filters ?? {})}`,
      ),
  })
}

export function useIssue(
  client: ApiClient,
  workspaceId: string,
  issueId: string,
) {
  return useQuery({
    queryKey: ['application', 'issue_pr', 'issues', workspaceId, issueId],
    queryFn: () =>
      client.request<Issue>(
        `/api/workspaces/${workspaceId}/issues/${issueId}`,
      ),
  })
}

export interface CreateIssueInput {
  title: string
  description?: string
  assignee_type?: string | null
  assignee_id?: string | null
  labels?: string[]
}

export function useCreateIssue(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateIssueInput) =>
      client.request<Issue>(`/api/workspaces/${workspaceId}/issues`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['application', 'issue_pr', 'issues', workspaceId] }),
  })
}

export interface UpdateIssueInput {
  status?: string
  assignee_type?: string | null
  assignee_id?: string | null
  labels?: string[]
}

export function useUpdateIssue(
  client: ApiClient,
  workspaceId: string,
  issueId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateIssueInput) =>
      client.request<Issue>(
        `/api/workspaces/${workspaceId}/issues/${issueId}`,
        { method: 'PATCH', body: JSON.stringify(input) },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['application', 'issue_pr', 'issues', workspaceId] })
      queryClient.invalidateQueries({
        queryKey: ['application', 'issue_pr', 'issues', workspaceId, issueId],
      })
    },
  })
}

export interface AddCommentInput {
  body: string
}

export function useAddComment(
  client: ApiClient,
  workspaceId: string,
  issueId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: AddCommentInput) =>
      client.request<IssueComment>(
        `/api/workspaces/${workspaceId}/issues/${issueId}/comments`,
        { method: 'POST', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['application', 'issue_pr', 'issues', workspaceId, issueId],
      }),
  })
}

export function useMoveIssue(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { issueId: string; status: string }) =>
      client.request<Issue>(
        `/api/workspaces/${workspaceId}/issues/${input.issueId}`,
        { method: 'PATCH', body: JSON.stringify({ status: input.status }) },
      ),
    // Phase A.3 (§5.3): optimistic update — move the card in the cache
    // *before* the PATCH resolves so the column snaps immediately. The
    // snapshot is restored on error so a failed move rolls the card
    // back to its origin column without a refetch round-trip. A
    // concurrent WS-driven status change (§5.4.3) also lands in the
    // same cache, so the optimistic move and the authoritative move
    // share one source of truth.
    onMutate: async (input) => {
      await queryClient.cancelQueries({ queryKey: ['application', 'issue_pr', 'issues', workspaceId] })
      const previous = queryClient.getQueriesData<Issue[]>({
        queryKey: ['application', 'issue_pr', 'issues', workspaceId],
      })
      queryClient.setQueriesData<Issue[]>(
        { queryKey: ['application', 'issue_pr', 'issues', workspaceId] },
        (current) =>
          (current ?? []).map((issue) =>
            issue.id === input.issueId
              ? { ...issue, status: input.status as Issue['status'] }
              : issue,
          ),
      )
      return { previous }
    },
    onError: (_err, _input, context) => {
      const snapshot = context as
        | { previous: Array<[readonly unknown[], Issue[] | undefined]> }
        | undefined
      if (!snapshot) return
      for (const [key, value] of snapshot.previous) {
        queryClient.setQueryData<Issue[]>(key as readonly unknown[], value)
      }
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ['application', 'issue_pr', 'issues', workspaceId] }),
  })
}

export function useSessionsByIssue(client: ApiClient, issueId: string) {
  return useQuery({
    queryKey: ['application', 'issue_pr', 'sessions', issueId],
    queryFn: async () => (await client.request<Parameters<typeof adaptSession>[0][]>(`/api/issues/${issueId}/sessions`)).map(adaptSession),
  })
}
