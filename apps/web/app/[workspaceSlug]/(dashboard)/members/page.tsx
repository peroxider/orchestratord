'use client'

import { useParams } from 'next/navigation'
import { MembersList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function MembersRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return <MembersList client={apiClient} workspaceId={params.workspaceSlug} />
}
