'use client'

import { useState } from 'react'
import {
  useAutopilots,
  useCreateAutopilot,
  usePatchAutopilot,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'

export interface AutopilotsListProps {
  client: ApiClient
  workspaceId: string
}

export function AutopilotsList({ client, workspaceId }: AutopilotsListProps) {
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [prompt, setPrompt] = useState('')
  const [targetKind, setTargetKind] = useState('issue')
  const [targetId, setTargetId] = useState('')

  const { data, isPending, isError, error } = useAutopilots(client, workspaceId)
  const create = useCreateAutopilot(client, workspaceId)

  function submitCreate() {
    if (!name.trim() || !cron.trim() || !targetId.trim()) return
    create.mutate(
      {
        name: name.trim(),
        cron: cron.trim(),
        prompt: prompt.trim(),
        target_kind: targetKind,
        target_id: targetId.trim(),
        enabled: true,
      },
      { onSuccess: () => setName('') },
    )
  }

  return (
    <div className="autopilots">
      <Card className="autopilots__create">
        <div className="autopilots__create-form">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name"
            aria-label="Name"
          />
          <Input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            placeholder="Cron (e.g. 0 * * * *)"
            aria-label="Cron"
          />
          <Input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Prompt"
            aria-label="Prompt"
          />
          <select
            value={targetKind}
            onChange={(e) => setTargetKind(e.target.value)}
            aria-label="Target kind"
          >
            <option value="issue">issue</option>
            <option value="squad">squad</option>
          </select>
          <Input
            value={targetId}
            onChange={(e) => setTargetId(e.target.value)}
            placeholder="Target id"
            aria-label="Target id"
          />
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim() || !cron.trim() || !targetId.trim()}
            onClick={submitCreate}
          >
            Create
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="autopilots__empty">Loading autopilots…</p>
      ) : isError ? (
        <p className="autopilots__empty">
          Failed to load autopilots: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="autopilots__empty">No autopilots yet.</p>
      ) : (
        <div className="autopilots__grid">
          {(data ?? []).map((autopilot) => (
            <AutopilotCard
              key={autopilot.id}
              workspaceId={workspaceId}
              client={client}
              autopilotId={autopilot.id}
              name={autopilot.name}
              cron={autopilot.cron}
              targetKind={autopilot.target_kind}
              enabled={autopilot.enabled}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AutopilotCard({
  workspaceId,
  client,
  autopilotId,
  name,
  cron,
  targetKind,
  enabled,
}: {
  workspaceId: string
  client: ApiClient
  autopilotId: string
  name: string
  cron: string
  targetKind: string
  enabled: boolean
}) {
  const patch = usePatchAutopilot(client, workspaceId, autopilotId)
  return (
    <Card className="autopilot-card">
      <header className="autopilot-card__header">
        <span className="autopilot-card__name">{name}</span>
        <Badge tone={enabled ? 'good' : 'neutral'}>
          {enabled ? 'enabled' : 'disabled'}
        </Badge>
      </header>
      <p className="autopilot-card__cron">{cron}</p>
      <p className="autopilot-card__target">Target: {targetKind}</p>
      <div className="autopilot-card__actions">
        <Button
          size="sm"
          variant={enabled ? 'secondary' : 'primary'}
          disabled={patch.isPending}
          onClick={() => patch.mutate({ enabled: !enabled })}
        >
          {enabled ? 'Disable' : 'Enable'}
        </Button>
      </div>
    </Card>
  )
}
