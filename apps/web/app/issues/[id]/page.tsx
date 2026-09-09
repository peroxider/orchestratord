'use client'
import { use } from 'react'
import { IssueApplicationDetailRoute } from '@/lib/registered-applications'
import { useInstanceContext } from '@/components/app-shell'
export default function Page({ params }: { params: Promise<{ id: string }> }) { const { id } = use(params); const i = useInstanceContext(); return <IssueApplicationDetailRoute workspaceId={i.workspace_id} id={id} /> }
