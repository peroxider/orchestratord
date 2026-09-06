'use client'

import { useParams } from 'next/navigation'
import { SquadsList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function SquadsRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <SquadsList client={apiClient} workspaceId={params.workspaceSlug} />
}
