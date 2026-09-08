'use client'

import { useAgents } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { CapabilityMatrix } from './capability-matrix'

export interface AgentsListProps {
  client: ApiClient
  workspaceId: string
}

export function AgentsList({ client, workspaceId }: AgentsListProps) {
  const { data, isPending, isError, error } = useAgents(client, workspaceId)

  if (isPending) {
    return <p className="agents__empty">Loading agents…</p>
  }
  if (isError) {
    return (
      <p className="agents__empty">
        Failed to load agents: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const agents = data ?? []
  if (agents.length === 0) {
    return <p className="agents__empty">No agents yet.</p>
  }

  return (
    <div className="agents">
      {agents.map((agent) => (
        <Card key={agent.id} className="agent-card">
          <header className="agent-card__header">
            <span className="agent-card__name">{agent.name}</span>
            <Badge tone="accent">{agent.provider}</Badge>
          </header>
          <CapabilityMatrix capabilities={agent.capabilities_cache_jsonb} />
        </Card>
      ))}
    </div>
  )
}
