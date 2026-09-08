import { useQuery } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { InstanceBootstrap } from '../api/types'

export function useInstance(client: ApiClient) {
  return useQuery({
    queryKey: ['instance'],
    queryFn: () => client.request<InstanceBootstrap>('/api/instance'),
    staleTime: Number.POSITIVE_INFINITY,
    retry: 2,
  })
}
