'use client'

import { useState } from 'react'
import {
  useCreateSquad,
  useDeleteSquad,
  useSquads,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'

export interface SquadsListProps {
  client: ApiClient
  workspaceId: string
}

export function SquadsList({ client, workspaceId }: SquadsListProps) {
  const [name, setName] = useState('')
  const [leaderType, setLeaderType] = useState('member')
  const [leaderId, setLeaderId] = useState('')

  const { data, isPending, isError, error } = useSquads(client, workspaceId)
  const create = useCreateSquad(client, workspaceId)

  function submitCreate() {
    if (!name.trim() || !leaderId.trim()) return
    create.mutate(
      { name: name.trim(), leader_type: leaderType, leader_id: leaderId.trim() },
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
            placeholder="Squad name"
            aria-label="Squad name"
          />
          <select
            value={leaderType}
            onChange={(e) => setLeaderType(e.target.value)}
            aria-label="Leader type"
          >
            <option value="member">member</option>
            <option value="agent">agent</option>
          </select>
          <Input
            value={leaderId}
            onChange={(e) => setLeaderId(e.target.value)}
            placeholder="Leader id"
            aria-label="Leader id"
          />
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim() || !leaderId.trim()}
            onClick={submitCreate}
          >
            Create
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="squads__empty">Loading squads…</p>
      ) : isError ? (
        <p className="squads__empty">
          Failed to load squads: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="squads__empty">No squads yet.</p>
      ) : (
        <div className="squads__grid">
          {(data ?? []).map((squad) => (
            <SquadCard
              key={squad.id}
              name={squad.name}
              leaderType={squad.leader_type}
              leaderId={squad.leader_id}
              memberCount={squad.members.length}
              workspaceId={workspaceId}
              client={client}
              squadId={squad.id}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function SquadCard({
  name,
  leaderType,
  leaderId,
  memberCount,
  workspaceId,
  client,
  squadId,
}: {
  name: string
  leaderType: string
  leaderId: string
  memberCount: number
  workspaceId: string
  client: ApiClient
  squadId: string
}) {
  const remove = useDeleteSquad(client, workspaceId, squadId)
  return (
    <Card className="squad-card">
      <header className="squad-card__header">
        <span className="squad-card__name">{name}</span>
        <Badge tone="accent">{leaderType}</Badge>
      </header>
      <p className="squad-card__leader">Leader: {leaderId}</p>
      <p className="squad-card__members">{memberCount} member(s)</p>
      <div className="squad-card__actions">
        <Button
          size="sm"
          variant="ghost"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete
        </Button>
      </div>
    </Card>
  )
}
