import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type {
  ChatMessage,
  ChatSessionMessages,
  ChatSessionStart,
} from '../api/types'

const MESSAGES_KEY = 'chat-messages'

/**
 * Chat timeline for one session. Refetches on a short interval so assistant
 * turns folded in by the §6.1d dispatcher appear without a manual refresh;
 * the WS bridge (§5.1) can replace polling when wired client-side.
 */
export function useSessionMessages(client: ApiClient, sessionId: string | null) {
  return useQuery({
    queryKey: [MESSAGES_KEY, sessionId],
    enabled: sessionId != null,
    refetchInterval: sessionId ? 2000 : false,
    queryFn: () =>
      client.request<ChatSessionMessages>(
        `/api/sessions/${sessionId}/messages`,
      ),
  })
}

export interface StartChatSessionInput {
  prompt: string
  agent_id?: string | null
}

export function useStartChatSession(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: StartChatSessionInput) =>
      client.request<ChatSessionStart>(
        `/api/workspaces/${workspaceId}/chat/sessions`,
        {
          method: 'POST',
          body: JSON.stringify(input),
        },
      ),
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: [MESSAGES_KEY, data.session_id],
      })
    },
  })
}

export interface SendChatMessageInput {
  content: string
}

export function useSendChatMessage(client: ApiClient, sessionId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: SendChatMessageInput) =>
      client.request<ChatMessage>(`/api/sessions/${sessionId}/messages`, {
        method: 'POST',
        body: JSON.stringify({
          role: 'user',
          content: input.content,
          author_label: 'me',
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [MESSAGES_KEY, sessionId] })
    },
  })
}
