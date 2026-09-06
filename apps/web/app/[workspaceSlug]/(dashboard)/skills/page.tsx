'use client'

import { useParams } from 'next/navigation'
import { SkillsList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function SkillsPage() {
  const params = useParams<{ workspaceSlug: string }>()
  return <SkillsList client={apiClient} workspaceId={params.workspaceSlug} />
}
