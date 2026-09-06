'use client'

import { useParams } from 'next/navigation'
import { UsagePage } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function UsageRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <UsagePage client={apiClient} workspaceId={params.workspaceSlug} />
}
