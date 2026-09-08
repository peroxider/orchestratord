import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { InboxItem, InboxItemStatus } from '../api/types'

export function useInbox(
  client: ApiClient,
  workspaceId: string,
  status?: InboxItemStatus,
) {
  return useQuery({
    queryKey: ['inbox', workspaceId, status],
    queryFn: () =>
      client.request<InboxItem[]>(
        `/api/workspaces/${workspaceId}/inbox${status ? `?status=${status}` : ''}`,
      ),
  })
}

function useInboxAction(
  client: ApiClient,
  workspaceId: string,
  action: 'assign' | 'resolve' | 'dismiss',
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { itemId: string; body?: unknown }) =>
      client.request<InboxItem>(
        `/api/workspaces/${workspaceId}/inbox/${input.itemId}/${action}`,
        {
          method: 'POST',
          body: input.body === undefined ? undefined : JSON.stringify(input.body),
        },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['inbox', workspaceId] }),
  })
}

export function useAssignInbox(client: ApiClient, workspaceId: string) {
  return useInboxAction(client, workspaceId, 'assign')
}

export function useResolveInbox(client: ApiClient, workspaceId: string) {
  return useInboxAction(client, workspaceId, 'resolve')
}

export function useDismissInbox(client: ApiClient, workspaceId: string) {
  return useInboxAction(client, workspaceId, 'dismiss')
}
