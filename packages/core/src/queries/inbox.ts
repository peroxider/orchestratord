import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { InboxItem, InboxItemStatus, SessionEvent, SessionEventsPage } from '../api/types'
import { adaptInboxItem } from '../api/adapters'

export function inboxApprovalRequestId(events: SessionEvent[], eventSeq: number | null): string | null {
  const event = eventSeq == null
    ? [...events].reverse().find(candidate => candidate.kind === 'approval_request')
    : events.find(candidate => candidate.seq === eventSeq)
  const requestId = event?.payload.request_id
  return event?.kind === 'approval_request' && typeof requestId === 'string' && requestId ? requestId : null
}

export function useInbox(
  client: ApiClient,
  workspaceId: string,
  status?: InboxItemStatus,
) {
  return useQuery({
    queryKey: ['inbox', workspaceId, status],
    queryFn: async () =>
      (await client.request<Parameters<typeof adaptInboxItem>[0][]>(
        `/api/workspaces/${workspaceId}/inbox${status ? `?status=${status}` : ''}`,
      )).map(adaptInboxItem),
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

export function useInboxDecision(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (input: { item: InboxItem; decision: 'approve' | 'deny' }) => {
      if (!input.item.session_id) throw new Error('This approval is not linked to a session.')
      const eventQuery = input.item.event_seq == null ? '?limit=200' : `?from_seq=${input.item.event_seq}&to_seq=${input.item.event_seq}&limit=1`
      const page = await client.request<SessionEventsPage>(`/api/sessions/${input.item.session_id}/events${eventQuery}`)
      const requestId = inboxApprovalRequestId(page.events, input.item.event_seq)
      if (!requestId) {
        throw new Error('The approval request could not be found. Open the session to inspect its current state.')
      }
      await client.request(`/api/sessions/${input.item.session_id}/${input.decision}`, { method: 'POST', body: JSON.stringify({ request_id: requestId }) })
      return client.request<InboxItem>(`/api/workspaces/${workspaceId}/inbox/${input.item.id}/${input.decision === 'approve' ? 'resolve' : 'dismiss'}`, { method: 'POST' })
    },
    onSuccess: (_data, input) => {
      queryClient.invalidateQueries({ queryKey: ['inbox', workspaceId] })
      if (input.item.session_id) queryClient.invalidateQueries({ queryKey: ['sessions', input.item.session_id, 'events'] })
    },
  })
}

export function useAnswerInboxClarification(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { itemId: string; answer: string }) => client.request<InboxItem>(`/api/workspaces/${workspaceId}/inbox/${input.itemId}/answer`, { method: 'POST', body: JSON.stringify({ answer: input.answer }) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['inbox', workspaceId] }),
  })
}
