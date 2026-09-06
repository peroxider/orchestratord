'use client'

import { useParams } from 'next/navigation'
import { AutopilotsList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function AutopilotsRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <AutopilotsList client={apiClient} workspaceId={params.workspaceSlug} />
}
