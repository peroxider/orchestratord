'use client'

import { useParams } from 'next/navigation'
import { AuditList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function AuditRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <AuditList client={apiClient} workspaceId={params.workspaceSlug} />
}
