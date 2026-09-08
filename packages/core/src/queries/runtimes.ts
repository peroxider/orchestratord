import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Runtime } from '../api/types'

export function useRuntimes(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['runtimes', workspaceId],
    queryFn: () =>
      client.request<Runtime[]>(`/api/workspaces/${workspaceId}/runtimes`),
  })
}

export function useRuntime(client: ApiClient, workspaceId: string, runtimeId: string) {
  return useQuery({
    queryKey: ['runtimes', workspaceId, runtimeId],
    queryFn: () => client.request<Runtime>(`/api/workspaces/${workspaceId}/runtimes/${runtimeId}`),
    enabled: Boolean(runtimeId),
  })
}

export interface RegisterRuntimeInput {
  hostname: string
  os: string
}

export interface RegisteredRuntime extends Runtime {
  token: string
}

export function useRegisterRuntime(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: RegisterRuntimeInput) =>
      client.request<RegisteredRuntime>(
        `/api/workspaces/${workspaceId}/runtimes`,
        { method: 'POST', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['runtimes', workspaceId] }),
  })
}

export function useRevokeRuntime(
  client: ApiClient,
  workspaceId: string,
  runtimeId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<Runtime>(
        `/api/workspaces/${workspaceId}/runtimes/${runtimeId}/revoke`,
        { method: 'POST' },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['runtimes', workspaceId] }),
  })
}
