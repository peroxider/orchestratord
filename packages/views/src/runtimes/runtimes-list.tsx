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
  const [copied, setCopied] = useState(false)

  const { data, isPending, isError, error } = useRuntimes(client, workspaceId)
  const register = useRegisterRuntime(client, workspaceId)
  const { locale } = useTranslation()
  const c = runtimeCopy[locale]

  function submitRegister() {
    if (!hostname.trim()) return
    register.mutate(
      { hostname: hostname.trim(), os: os.trim() },
      {
        onSuccess: (runtime) => {
          setIssuedToken(runtime.token)
          setCopied(false)
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
            placeholder={c.hostname}
            aria-label={c.hostname}
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
            {c.register}
          </Button>
        </div>
        {issuedToken && (
          <div className="runtimes__token" role="status">
            <span>{c.oneTime}: <code>{issuedToken}</code></span>
            <Button size="sm" variant="secondary" onClick={() => void navigator.clipboard.writeText(issuedToken).then(() => setCopied(true))}>{copied ? c.copied : c.copy}</Button>
          </div>
        )}
      </Card>

      {isPending ? (
        <p className="runtimes__empty">{c.loading}</p>
      ) : isError ? (
        <p className="runtimes__empty">
          {c.failed}: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="runtimes__empty">{c.empty}</p>
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
  const c = runtimeCopy[locale]
  return (
    <Card className="runtime-card">
      <header className="runtime-card__header">
        <a className="runtime-card__hostname" href={`/runtimes/${runtime.id}`}>{runtime.hostname}</a>
        <Badge tone={RUNTIME_STATUS_TONE[runtime.status] ?? 'neutral'}>
          {runtimeStatusLabel(runtime.status, locale)}
        </Badge>
      </header>
      <p className="runtime-card__os">{runtime.os}</p>
      {runtime.last_seen_at && (
        <p className="runtime-card__meta">
          {c.lastSeen}: {new Date(runtime.last_seen_at).toLocaleString(locale)}
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
            onClick={() => { if (window.confirm(`${c.revokeConfirm} “${runtime.hostname}”?`)) revoke.mutate() }}
          >
            {c.revoke}
          </Button>
        </div>
      )}
    </Card>
  )
}

const runtimeCopy = {
  en: { hostname: 'Hostname', register: 'Register', oneTime: 'One-time token (copy now)', copied: 'Copied', copy: 'Copy token', loading: 'Loading runtimes…', failed: 'Failed to load runtimes', empty: 'No runtimes registered.', lastSeen: 'Last seen', revoke: 'Revoke', revokeConfirm: 'Revoke this Runtime and invalidate its credential' },
  'zh-CN': { hostname: '主机名', register: '注册', oneTime: '一次性 Token（请立即复制）', copied: '已复制', copy: '复制 Token', loading: '正在加载运行时…', failed: '无法加载运行时', empty: '尚未注册运行时。', lastSeen: '最后在线', revoke: '吊销', revokeConfirm: '吊销此运行时并使其凭据失效' },
  ja: { hostname: 'ホスト名', register: '登録', oneTime: '一度だけ表示されるトークン（今すぐコピー）', copied: 'コピー済み', copy: 'トークンをコピー', loading: 'ランタイムを読み込み中…', failed: 'ランタイムを読み込めませんでした', empty: 'ランタイムは未登録です。', lastSeen: '最終確認', revoke: '無効化', revokeConfirm: 'このランタイムと認証情報を無効化しますか' },
} as const
