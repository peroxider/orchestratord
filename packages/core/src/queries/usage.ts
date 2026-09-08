import { useQuery } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { AgentUsageResponse, UsageResponse } from '../api/types'

export type UsageDimension = 'agent' | 'backend' | 'issue' | 'day' | 'workspace'

export interface UsageWindow {
  from?: string
  to?: string
  group_by?: UsageDimension
}

function toUsageQueryString(window: UsageWindow): string {
  const params = new URLSearchParams()
  if (window.from) params.set('from', window.from)
  if (window.to) params.set('to', window.to)
  if (window.group_by) params.set('group_by', window.group_by)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function useWorkspaceUsage(
  client: ApiClient,
  workspaceId: string,
  window: UsageWindow = {},
) {
  return useQuery({
    queryKey: ['usage', workspaceId, window],
    queryFn: () =>
      client.request<UsageResponse>(
        `/api/workspaces/${workspaceId}/usage${toUsageQueryString(window)}`,
      ),
  })
}

export function useAgentUsage(
  client: ApiClient,
  agentId: string,
  window: UsageWindow = {},
) {
  return useQuery({
    queryKey: ['usage', 'agent', agentId, window],
    queryFn: () =>
      client.request<AgentUsageResponse>(
        `/api/agents/${agentId}/usage${toUsageQueryString(window)}`,
      ),
  })
}
