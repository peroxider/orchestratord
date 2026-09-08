import { useQuery } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { PullRequestsResponse } from '../api/types'

export function usePullRequests(client: ApiClient, issueId: string) {
  return useQuery({
    queryKey: ['pull-requests', issueId],
    queryFn: () =>
      client.request<PullRequestsResponse>(`/api/issues/${issueId}/pull-requests`),
  })
}
