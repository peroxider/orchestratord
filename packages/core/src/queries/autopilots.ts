import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Autopilot } from '../api/types'

export function useAutopilots(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['autopilots', workspaceId],
    queryFn: () =>
      client.request<Autopilot[]>(`/api/workspaces/${workspaceId}/autopilots`),
  })
}

export function useAutopilot(
  client: ApiClient,
  workspaceId: string,
  autopilotId: string,
) {
  return useQuery({
    queryKey: ['autopilots', workspaceId, autopilotId],
    queryFn: () =>
      client.request<Autopilot>(
        `/api/workspaces/${workspaceId}/autopilots/${autopilotId}`,
      ),
  })
}

export interface CreateAutopilotInput {
  name: string
  cron: string
  prompt: string
  target_kind: string
  target_id: string
  enabled?: boolean
}

export function useCreateAutopilot(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateAutopilotInput) =>
      client.request<Autopilot>(`/api/workspaces/${workspaceId}/autopilots`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['autopilots', workspaceId] }),
  })
}

export function usePatchAutopilot(
  client: ApiClient,
  workspaceId: string,
  autopilotId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { enabled: boolean }) =>
      client.request<Autopilot>(
        `/api/workspaces/${workspaceId}/autopilots/${autopilotId}`,
        { method: 'PATCH', body: JSON.stringify(input) },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['autopilots', workspaceId] })
      queryClient.invalidateQueries({
        queryKey: ['autopilots', workspaceId, autopilotId],
      })
    },
  })
}
