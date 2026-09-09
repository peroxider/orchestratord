import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Session, SessionEventsPage } from '../api/types'
import { adaptSession } from '../api/adapters'

export function useSessions(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['sessions', 'workspace', workspaceId],
    queryFn: async () => (await client.request<Parameters<typeof adaptSession>[0][]>(`/api/workspaces/${workspaceId}/sessions`)).map(adaptSession),
  })
}

export function useSession(client: ApiClient, sessionId: string) {
  return useQuery({
    queryKey: ['sessions', sessionId],
    queryFn: async () => adaptSession(await client.request<Parameters<typeof adaptSession>[0]>(`/api/sessions/${sessionId}`)),
  })
}

export function useSessionEvents(client: ApiClient, sessionId: string) {
  return useQuery({
    queryKey: ['sessions', sessionId, 'events'],
    queryFn: () =>
      client.request<SessionEventsPage>(
        `/api/sessions/${sessionId}/events`,
      ),
  })
}

export function useSessionDecision(
  client: ApiClient,
  sessionId: string,
  decision: 'approve' | 'deny',
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (requestId: string) =>
      client.request(`/api/sessions/${sessionId}/${decision}`, {
        method: 'POST',
        body: JSON.stringify({ request_id: requestId }),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['sessions', sessionId, 'events'],
      }),
  })
}

export function useSessionControl(
  client: ApiClient,
  sessionId: string,
  action: 'pause' | 'resume' | 'stop',
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<Session>(`/api/sessions/${sessionId}/${action}`, {
        method: 'POST',
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['sessions', sessionId] }),
  })
}
