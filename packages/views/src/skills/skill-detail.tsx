'use client'

import { useSkill, useVerifySkill } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'

export interface SkillDetailProps {
  client: ApiClient
  name: string
}

export function SkillDetail({ client, name }: SkillDetailProps) {
  const skill = useSkill(client, name)
  const verify = useVerifySkill(client, name)

  if (skill.isPending) {
    return <p className="skill-detail__empty">Loading skill…</p>
  }
  if (skill.isError) {
    return (
      <p className="skill-detail__empty">
        Failed to load skill: {skill.error?.message ?? 'unknown error'}
      </p>
    )
  }

  const data = skill.data

  return (
    <div className="skill-detail">
      <header className="skill-detail__header">
        <h2 className="skill-detail__title">{data.display_name}</h2>
        <Badge tone={data.is_stale ? 'warn' : 'good'}>
          {data.is_stale ? 'stale' : 'verified'}
        </Badge>
      </header>
      <p className="skill-detail__description">{data.description}</p>

      {data.stale_reasons.length > 0 && (
        <ul className="skill-detail__stale">
          {data.stale_reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}

      <div className="skill-detail__actions">
        <Button
          size="sm"
          variant="secondary"
          disabled={verify.isPending}
          onClick={() => verify.mutate()}
        >
          Verify
        </Button>
        {verify.data && (
          <Badge tone={verify.data.verified ? 'good' : 'warn'}>
            {verify.data.verified ? 'verified' : 'stale'}
          </Badge>
        )}
      </div>

      <section className="skill-detail__source-map">
        <h3>Source map</h3>
        <ul>
          {data.source_map.map((ref) => (
            <li key={`${ref.file_path}:${ref.start_line}`}>
              <span className="skill-detail__ref">
                {ref.file_path}:{ref.start_line}-{ref.end_line}
              </span>
              <code className="skill-detail__hash">
                {ref.expected_sha256_prefix}
              </code>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
