import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Squad } from '../api/types'

export function useSquads(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['squads', workspaceId],
    queryFn: () =>
      client.request<Squad[]>(`/api/workspaces/${workspaceId}/squads`),
  })
}

export function useSquad(
  client: ApiClient,
  workspaceId: string,
  squadId: string,
) {
  return useQuery({
    queryKey: ['squads', workspaceId, squadId],
    queryFn: () =>
      client.request<Squad>(`/api/workspaces/${workspaceId}/squads/${squadId}`),
  })
}

export interface CreateSquadInput {
  name: string
  leader_type: string
  leader_id: string
  members?: { member_type: string; member_id: string }[]
}

export function useCreateSquad(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateSquadInput) =>
      client.request<Squad>(`/api/workspaces/${workspaceId}/squads`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['squads', workspaceId] }),
  })
}

export function useDeleteSquad(
  client: ApiClient,
  workspaceId: string,
  squadId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<void>(`/api/workspaces/${workspaceId}/squads/${squadId}`, {
        method: 'DELETE',
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['squads', workspaceId] }),
  })
}
