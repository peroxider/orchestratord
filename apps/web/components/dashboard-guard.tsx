'use client'

import { useEffect } from 'react'
import type { ReactNode } from 'react'
import {
  CoreProvider,
  useRealtimeBridge,
  useWorkspaceStore,
} from '@orchestratord/core'
import { I18nProvider, LocaleSwitcher } from '@orchestratord/views'
import { getToken } from '@/lib/auth'
import { realtimeUrl } from '@/lib/realtime'

export function DashboardGuard({
  slug,
  children,
}: {
  slug: string
  children: ReactNode
}) {
  return (
    <CoreProvider>
      <I18nProvider>
        <WorkspaceShell slug={slug}>{children}</WorkspaceShell>
      </I18nProvider>
    </CoreProvider>
  )
}

function WorkspaceShell({
  slug,
  children,
}: {
  slug: string
  children: ReactNode
}) {
  const setCurrentWorkspace = useWorkspaceStore((s) => s.setCurrentWorkspace)

  useEffect(() => {
    // Phase 1: the URL slug doubles as the workspace id until slug→uuid
    // resolution lands (docs §5.4.2 / Phase 2).
    setCurrentWorkspace(slug, slug)
  }, [slug, setCurrentWorkspace])

  // The realtime gate now validates this token against ``auth_tokens`` — it
  // is the same credential the REST console sends as ``Authorization: Bearer``.
  useRealtimeBridge({
    url: realtimeUrl,
    workspaceId: slug,
    token: getToken() ?? '',
  })

  return (
    <div className="workspace-shell">
      <header className="workspace-shell__header">
        <span className="workspace-shell__slug">{slug}</span>
        <LocaleSwitcher />
      </header>
      <main className="workspace-shell__main">{children}</main>
    </div>
  )
}
