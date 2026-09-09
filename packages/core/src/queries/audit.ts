import { useQuery } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { AuditActorType, AuditLogEntry } from '../api/types'
import { adaptAuditEntry } from '../api/adapters'

export interface AuditFilters {
  actor_type?: AuditActorType
  target_type?: string
  action?: string
  from?: string
  to?: string
}

export function toAuditQueryString(filters: AuditFilters): string {
  const params = new URLSearchParams()
  if (filters.actor_type) params.set('actor_type', filters.actor_type)
  if (filters.target_type) params.set('target_type', filters.target_type)
  if (filters.action) params.set('action', filters.action)
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function useAudit(
  client: ApiClient,
  workspaceId: string,
  filters?: AuditFilters,
) {
  return useQuery({
    queryKey: ['audit', workspaceId, filters],
    queryFn: async () =>
      (await client.request<Parameters<typeof adaptAuditEntry>[0][]>(
        `/api/workspaces/${workspaceId}/audit${toAuditQueryString(filters ?? {})}`,
      )).map(adaptAuditEntry),
  })
}
