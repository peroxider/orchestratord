'use client'

import { useWorkspaceStore } from '@orchestratord/core'
import { ChatPage } from '@orchestratord/views'
import { apiClient } from '@/lib/api'

export function ChatPageScreen({ slug }: { slug: string }) {
  const workspaceId = useWorkspaceStore((s) => s.workspaceId) ?? slug
  return <ChatPage client={apiClient} workspaceId={workspaceId} />
}
