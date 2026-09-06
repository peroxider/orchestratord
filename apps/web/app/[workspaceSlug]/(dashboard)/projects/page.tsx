'use client'

import { useParams } from 'next/navigation'
import { ProjectsList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function ProjectsRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <ProjectsList client={apiClient} workspaceId={params.workspaceSlug} />
}
