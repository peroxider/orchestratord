import { useQuery } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Agent } from '../api/types'

export function useAgents(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['agents', workspaceId],
    queryFn: () =>
      client.request<Agent[]>(`/api/workspaces/${workspaceId}/agents`),
  })
}

export function useAgent(client: ApiClient, workspaceId: string, agentId: string) {
  return useQuery({
    queryKey: ['agents', workspaceId],
    queryFn: () => client.request<Agent[]>(`/api/workspaces/${workspaceId}/agents`),
    select: (agents) => agents.find((agent) => agent.id === agentId) ?? null,
  })
}
