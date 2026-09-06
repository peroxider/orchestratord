import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Member, MemberScopes } from '../api/types'

export function useMembers(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      client.request<Member[]>(`/api/workspaces/${workspaceId}/members`),
  })
}

export interface CreateMemberInput {
  role: Member['role']
  name?: string
}

export function useCreateMember(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateMemberInput) =>
      client.request<Member>(`/api/workspaces/${workspaceId}/members`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['members', workspaceId] }),
  })
}

export interface UpdateMemberInput {
  role?: Member['role']
  name?: string
}

export function useUpdateMember(
  client: ApiClient,
  workspaceId: string,
  memberId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateMemberInput) =>
      client.request<Member>(
        `/api/workspaces/${workspaceId}/members/${memberId}`,
        { method: 'PATCH', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['members', workspaceId] }),
  })
}

export function useDeleteMember(
  client: ApiClient,
  workspaceId: string,
  memberId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<void>(`/api/workspaces/${workspaceId}/members/${memberId}`, {
        method: 'DELETE',
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['members', workspaceId] }),
  })
}

export function useMemberScopes(
  client: ApiClient,
  workspaceId: string,
  memberId: string,
) {
  return useQuery({
    queryKey: ['members', workspaceId, memberId, 'scopes'],
    queryFn: () =>
      client.request<MemberScopes>(
        `/api/workspaces/${workspaceId}/members/${memberId}/scopes`,
      ),
  })
}

export function useGrantScope(
  client: ApiClient,
  workspaceId: string,
  memberId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { agent_id: string }) =>
      client.request<{ member_id: string; agent_id: string }>(
        `/api/workspaces/${workspaceId}/members/${memberId}/scopes`,
        { method: 'POST', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['members', workspaceId, memberId, 'scopes'],
      }),
  })
}

export function useRevokeScope(
  client: ApiClient,
  workspaceId: string,
  memberId: string,
  agentId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<void>(
        `/api/workspaces/${workspaceId}/members/${memberId}/scopes/${agentId}`,
        { method: 'DELETE' },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['members', workspaceId, memberId, 'scopes'],
      }),
  })
}
