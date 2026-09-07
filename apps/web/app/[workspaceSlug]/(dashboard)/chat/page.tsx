import { ChatPageScreen } from '@/components/chat-screen'

export default async function ChatPage({
  params,
}: {
  params: Promise<{ workspaceSlug: string }>
}) {
  const { workspaceSlug } = await params
  return <ChatPageScreen slug={workspaceSlug} />
}
