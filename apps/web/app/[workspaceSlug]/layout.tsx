import type { ReactNode } from 'react'
import { DashboardGuard } from '@/components/dashboard-guard'

export default async function WorkspaceLayout({
  children,
  params,
}: {
  children: ReactNode
  params: Promise<{ workspaceSlug: string }>
}) {
  const { workspaceSlug } = await params
  return <DashboardGuard slug={workspaceSlug}>{children}</DashboardGuard>
}
