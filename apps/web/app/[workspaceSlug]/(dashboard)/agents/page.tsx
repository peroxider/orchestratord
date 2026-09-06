'use client'

import { useParams } from 'next/navigation'
import { AgentsList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function AgentsPage() {
  const params = useParams<{ workspaceSlug: string }>()
  return <AgentsList client={apiClient} workspaceId={params.workspaceSlug} />
}
