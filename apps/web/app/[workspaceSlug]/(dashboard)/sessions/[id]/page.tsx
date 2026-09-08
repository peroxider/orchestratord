'use client'

import { useParams } from 'next/navigation'
import { SessionDetail } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function SessionDetailPage() {
  const params = useParams<{ id: string }>()
  return <SessionDetail client={apiClient} sessionId={params.id} />
}
