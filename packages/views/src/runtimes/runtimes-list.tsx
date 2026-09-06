'use client'

import { useState } from 'react'
import {
  useRegisterRuntime,
  useRevokeRuntime,
  useRuntimes,
  type ApiClient,
  type Runtime,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { RUNTIME_STATUS_TONE, runtimeStatusLabel } from './runtime-status'

export interface RuntimesListProps {
  client: ApiClient
  workspaceId: string
}

export function RuntimesList({ client, workspaceId }: RuntimesListProps) {
  const [hostname, setHostname] = useState('')
  const [os, setOs] = useState('')
  const [issuedToken, setIssuedToken] = useState<string | null>(null)

  const { data, isPending, isError, error } = useRuntimes(client, workspaceId)
  const register = useRegisterRuntime(client, workspaceId)

  function submitRegister() {
    if (!hostname.trim()) return
    register.mutate(
      { hostname: hostname.trim(), os: os.trim() },
      {
        onSuccess: (runtime) => {
          setIssuedToken(runtime.token)
          setHostname('')
          setOs('')
        },
      },
    )
  }

  return (
    <div className="runtimes">
      <Card className="runtimes__register">
        <div className="runtimes__register-form">
          <Input
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="Hostname"
            aria-label="Hostname"
          />
          <Input
            value={os}
            onChange={(e) => setOs(e.target.value)}
            placeholder="OS"
            aria-label="OS"
          />
          <Button
            size="sm"
            variant="primary"
            disabled={register.isPending || !hostname.trim()}
            onClick={submitRegister}
          >
            Register
          </Button>
        </div>
        {issuedToken && (
          <p className="runtimes__token">
            One-time token (copy now): <code>{issuedToken}</code>
          </p>
        )}
      </Card>

      {isPending ? (
        <p className="runtimes__empty">Loading runtimes…</p>
      ) : isError ? (
        <p className="runtimes__empty">
          Failed to load runtimes: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="runtimes__empty">No runtimes registered.</p>
      ) : (
        <div className="runtimes__grid">
          {(data ?? []).map((runtime) => (
            <RuntimeCard
              key={runtime.id}
              runtime={runtime}
              workspaceId={workspaceId}
              client={client}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function RuntimeCard({
  runtime,
  workspaceId,
  client,
}: {
  runtime: Runtime
  workspaceId: string
  client: ApiClient
}) {
  const revoke = useRevokeRuntime(client, workspaceId, runtime.id)
  const { locale } = useTranslation()
  return (
    <Card className="runtime-card">
      <header className="runtime-card__header">
        <span className="runtime-card__hostname">{runtime.hostname}</span>
        <Badge tone={RUNTIME_STATUS_TONE[runtime.status]}>
          {runtimeStatusLabel(runtime.status, locale)}
        </Badge>
      </header>
      <p className="runtime-card__os">{runtime.os}</p>
      {runtime.last_seen_at && (
        <p className="runtime-card__meta">
          Last seen: {new Date(runtime.last_seen_at).toLocaleString()}
        </p>
      )}
      {runtime.probed_backends.length > 0 && (
        <ul className="runtime-card__backends">
          {runtime.probed_backends.map((b) => (
            <li key={b.name}>
              <Badge tone="accent">{b.name}</Badge>
              {b.version && <span className="runtime-card__version">{b.version}</span>}
            </li>
          ))}
        </ul>
      )}
      {runtime.status !== 'disabled' && (
        <div className="runtime-card__actions">
          <Button
            size="sm"
            variant="danger"
            disabled={revoke.isPending}
            onClick={() => revoke.mutate()}
          >
            Revoke
          </Button>
        </div>
      )}
    </Card>
  )
}
