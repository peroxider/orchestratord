'use client'

import { useState } from 'react'
import {
  useCreateSquad,
  useDeleteSquad,
  useAgents,
  useSquads,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'
import { useLocale } from '../i18n'

export interface SquadsListProps {
  client: ApiClient
  workspaceId: string
}

export function SquadsList({ client, workspaceId }: SquadsListProps) {
  const [name, setName] = useState('')
  const [leaderId, setLeaderId] = useState('')

  const { data, isPending, isError, error } = useSquads(client, workspaceId)
  const agents = useAgents(client, workspaceId)
  const create = useCreateSquad(client, workspaceId)
  const locale = useLocale()
  const c = squadCopy[locale]

  function submitCreate() {
    if (!name.trim() || !leaderId.trim()) return
    create.mutate(
      { name: name.trim(), leader_type: 'agent', leader_id: leaderId.trim() },
      { onSuccess: () => setName('') },
    )
  }

  return (
    <div className="squads">
      <Card className="squads__create">
        <div className="squads__create-form">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={c.name}
            aria-label={c.name}
          />
          <select
            value={leaderId}
            onChange={(e) => setLeaderId(e.target.value)}
            aria-label={c.coordinator}
          >
            <option value="">{c.select}</option>
            {(agents.data ?? []).map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </select>
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim() || !leaderId.trim()}
            onClick={submitCreate}
          >
            {c.create}
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="squads__empty">{c.loading}</p>
      ) : isError ? (
        <p className="squads__empty">
          {c.failed}: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="squads__empty">{c.empty}</p>
      ) : (
        <div className="squads__grid">
          {(data ?? []).map((squad) => (
            <SquadCard
              key={squad.id}
              name={squad.name}
              coordinator={(agents.data ?? []).find(agent => agent.id === squad.leader_id)?.name ?? squad.leader_id.slice(0, 8)}
              agentCount={squad.members.filter(member => member.member_type === 'agent').length + (squad.leader_type === 'agent' ? 1 : 0)}
              workspaceId={workspaceId}
              client={client}
              squadId={squad.id}
              copy={c}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function SquadCard({
  name,
  coordinator,
  agentCount,
  workspaceId,
  client,
  squadId,
  copy,
}: {
  name: string
  coordinator: string
  agentCount: number
  workspaceId: string
  client: ApiClient
  squadId: string
  copy: typeof squadCopy[keyof typeof squadCopy]
}) {
  const remove = useDeleteSquad(client, workspaceId, squadId)
  return (
    <Card className="squad-card">
      <header className="squad-card__header">
        <a className="squad-card__name" href={`/squads/${squadId}`}>{name}</a>
        <Badge tone="accent">{copy.badge}</Badge>
      </header>
      <p className="squad-card__leader">{copy.coordinator}: {coordinator}</p>
      <p className="squad-card__members">{agentCount} {copy.agents}</p>
      <div className="squad-card__actions">
        <Button
          size="sm"
          variant="ghost"
          disabled={remove.isPending}
          onClick={() => { if (window.confirm(`${copy.deleteConfirm} “${name}”?`)) remove.mutate() }}
        >
          {copy.delete}
        </Button>
      </div>
    </Card>
  )
}

const squadCopy = {
  en: { name: 'Squad name', coordinator: 'Coordinator', select: 'Select coordinator agent', create: 'Create', loading: 'Loading squads…', failed: 'Failed to load squads', empty: 'No agent squads yet.', badge: 'Agent squad', agents: 'agents', delete: 'Delete', deleteConfirm: 'Delete the agent squad' },
  'zh-CN': { name: '小组名称', coordinator: '协调 Agent', select: '选择协调 Agent', create: '创建', loading: '正在加载 Agent 小组…', failed: '无法加载 Agent 小组', empty: '暂无 Agent 小组。', badge: 'Agent 小组', agents: '个 Agent', delete: '删除', deleteConfirm: '删除 Agent 小组' },
  ja: { name: 'Squad 名', coordinator: 'コーディネーター', select: 'コーディネーターを選択', create: '作成', loading: 'Agent Squad を読み込み中…', failed: 'Agent Squad を読み込めませんでした', empty: 'Agent Squad はまだありません。', badge: 'Agent Squad', agents: 'エージェント', delete: '削除', deleteConfirm: 'Agent Squad を削除しますか' },
} as const
