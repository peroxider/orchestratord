import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { SkillDetail, SkillSummary } from '../api/types'

export function useSkills(client: ApiClient) {
  return useQuery({
    queryKey: ['skills'],
    queryFn: () => client.request<SkillSummary[]>('/api/skills'),
  })
}

export function useSkill(client: ApiClient, name: string) {
  return useQuery({
    queryKey: ['skills', name],
    queryFn: () => client.request<SkillDetail>(`/api/skills/${name}`),
  })
}

export interface SkillVerifyResult {
  verified: boolean
  stale_refs: string[]
}

export function useVerifySkill(client: ApiClient, name: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      client.request<SkillVerifyResult>(`/api/skills/${name}/verify`, {
        method: 'POST',
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['skills'] }),
  })
}
