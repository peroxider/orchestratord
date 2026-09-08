'use client'

import { useEffect } from 'react'
import type { ReactNode } from 'react'
import {
  CoreProvider,
  useRealtimeBridge,
  useWorkspaceStore,
} from '@orchestratord/core'
import { I18nProvider, LocaleSwitcher } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { getToken } from '@/lib/auth'
import { realtimeUrl } from '@/lib/realtime'

// Workspace-scoped API routes validate UUID path params (FastAPI UUID type),
// so a URL slug like "default" must be resolved to the real workspace id.
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

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
    // The URL carries the workspace slug; API routes need the UUID. Store
    // the slug provisionally, then swap in the real id via the lookup
    // route — every query key includes the id, so the swap refetches with
    // the valid value. A slug that already is a UUID is used as-is.
    setCurrentWorkspace(slug, slug)
    if (UUID_PATTERN.test(slug)) {
      return
    }
    let cancelled = false
    apiClient
      .request<{ workspace_id: string }>(
        `/api/workspaces/by-slug/${encodeURIComponent(slug)}`,
      )
      .then((workspace) => {
        if (!cancelled) {
          setCurrentWorkspace(slug, workspace.workspace_id)
        }
      })
      .catch(() => {
        // keep the provisional slug id; queries surface the API error
      })
    return () => {
      cancelled = true
    }
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
