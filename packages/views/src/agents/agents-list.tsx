'use client'

import { useAgents, useRuntimes, useSessions } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { CapabilityMatrix } from './capability-matrix'
import { RUNTIME_STATUS_TONE, runtimeStatusLabel } from '../runtimes/runtime-status'
import { useLocale } from '../i18n'

export interface AgentsListProps {
  client: ApiClient
  workspaceId: string
}

export function AgentsList({ client, workspaceId }: AgentsListProps) {
  const { data, isPending, isError, error } = useAgents(client, workspaceId)
  const runtimes = useRuntimes(client, workspaceId)
  const sessions = useSessions(client, workspaceId)
  const locale = useLocale()
  const c = {
    en: { loading: 'Loading agents…', failed: 'Failed to load agents', empty: 'No agents yet.', runtime: 'Runtime', unavailable: 'Unavailable', active: 'Active sessions', last: 'Last activity', never: 'No runs yet' },
    'zh-CN': { loading: '正在加载 Agent…', failed: '无法加载 Agent', empty: '暂无 Agent。', runtime: '运行时', unavailable: '不可用', active: '活跃会话', last: '最近活动', never: '尚无运行记录' },
    ja: { loading: 'エージェントを読み込み中…', failed: 'エージェントを読み込めませんでした', empty: 'エージェントはまだありません。', runtime: 'ランタイム', unavailable: '利用不可', active: '進行中のセッション', last: '最終アクティビティ', never: '実行履歴なし' },
  }[locale]

  if (isPending) {
    return <p className="agents__empty">{c.loading}</p>
  }
  if (isError) {
    return (
      <p className="agents__empty">
        {c.failed}: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const agents = data ?? []
  if (agents.length === 0) {
    return <p className="agents__empty">{c.empty}</p>
  }

  return (
    <div className="agents">
      {agents.map((agent) => {
        const runtime = runtimes.data?.find(item => item.id === agent.runtime_id)
        const agentSessions = (sessions.data ?? []).filter(item => item.agent_id === agent.id)
        const activeSessions = agentSessions.filter(item => ['pending', 'queued', 'running', 'paused', 'waiting'].includes(item.status)).length
        const lastActivity = [...agentSessions].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]?.created_at
        return <Card key={agent.id} className="agent-card">
          <header className="agent-card__header">
            <span className="agent-card__name">{agent.name}</span>
            <div><Badge tone="accent">{agent.provider}</Badge>{runtime && <Badge tone={RUNTIME_STATUS_TONE[runtime.status] ?? 'neutral'}>{runtimeStatusLabel(runtime.status, locale)}</Badge>}</div>
          </header>
          <dl className="agent-card__facts"><div><dt>{c.runtime}</dt><dd>{runtime?.hostname ?? c.unavailable}</dd></div><div><dt>{c.active}</dt><dd>{activeSessions}</dd></div><div><dt>{c.last}</dt><dd>{lastActivity ? new Date(lastActivity).toLocaleString(locale) : c.never}</dd></div></dl>
          <CapabilityMatrix capabilities={agent.capabilities_cache_jsonb} />
        </Card>
      })}
    </div>
  )
}
