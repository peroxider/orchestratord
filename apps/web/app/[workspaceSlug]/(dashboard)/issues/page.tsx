import { IssuesBoard } from '@/components/issues-board'

export default async function IssuesPage({
  params,
}: {
  params: Promise<{ workspaceSlug: string }>
}) {
  const { workspaceSlug } = await params
  return <IssuesBoard slug={workspaceSlug} />
}
