'use client'

import { useParams } from 'next/navigation'
import { IssueDetail } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { devAuthor } from '@/lib/identity'

export default function IssueDetailPage() {
  const params = useParams<{ workspaceSlug: string; id: string }>()
  return (
    <IssueDetail
      client={apiClient}
      workspaceId={params.workspaceSlug}
      issueId={params.id}
      authorType={devAuthor.author_type}
      authorId={devAuthor.author_id}
    />
  )
}
