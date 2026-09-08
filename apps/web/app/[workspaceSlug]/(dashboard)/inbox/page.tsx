'use client'

import { useParams } from 'next/navigation'
import { InboxList } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { DEV_MEMBER_ID } from '@/lib/identity'

export default function InboxRoute() {
  const params = useParams<{ workspaceSlug: string }>()
  return (
    <InboxList
      client={apiClient}
      workspaceId={params.workspaceSlug}
      currentMemberId={DEV_MEMBER_ID}
    />
  )
}
