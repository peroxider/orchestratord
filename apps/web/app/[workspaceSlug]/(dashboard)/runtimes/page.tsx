'use client'

import { useParams } from 'next/navigation'
import { RuntimesList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function RuntimesRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <RuntimesList client={apiClient} workspaceId={params.workspaceSlug} />
}
