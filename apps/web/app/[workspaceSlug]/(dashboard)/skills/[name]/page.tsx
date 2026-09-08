'use client'

import { useParams } from 'next/navigation'
import { SkillDetail } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export default function SkillDetailPage() {
  const params = useParams<{ name: string }>()
  return <SkillDetail client={apiClient} name={params.name} />
}
