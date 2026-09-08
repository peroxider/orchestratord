'use client'

import { useSkills, useVerifySkill } from '@orchestratord/core'
import type { ApiClient, SkillSummary } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'

export interface SkillsListProps {
  client: ApiClient
  workspaceId: string
}

export function SkillsList({ client, workspaceId }: SkillsListProps) {
  const { data, isPending, isError, error } = useSkills(client)

  if (isPending) {
    return <p className="skills__empty">Loading skills…</p>
  }
  if (isError) {
    return (
      <p className="skills__empty">
        Failed to load skills: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const skills = data ?? []
  if (skills.length === 0) {
    return <p className="skills__empty">No skills discovered.</p>
  }

  return (
    <div className="skills">
      {skills.map((skill) => (
        <SkillCard
          key={skill.name}
          client={client}
          workspaceId={workspaceId}
          skill={skill}
        />
      ))}
    </div>
  )
}

function SkillCard({
  client,
  workspaceId,
  skill,
}: {
  client: ApiClient
  workspaceId: string
  skill: SkillSummary
}) {
  const verify = useVerifySkill(client, skill.name)

  return (
    <Card className="skill-card">
      <header className="skill-card__header">
        <a
          className="skill-card__name"
          href={`/skills/${skill.name}`}
        >
          {skill.display_name}
        </a>
        <Badge tone={skill.is_stale ? 'warn' : 'good'}>
          {skill.is_stale ? 'stale' : 'verified'}
        </Badge>
      </header>
      <p className="skill-card__description">{skill.description}</p>
      <Button
        size="sm"
        variant="secondary"
        disabled={verify.isPending}
        onClick={() => verify.mutate()}
      >
        Verify
      </Button>
      {verify.data && !verify.data.verified && (
        <ul className="skill-card__stale">
          {verify.data.stale_refs.map((ref) => (
            <li key={ref}>{ref}</li>
          ))}
        </ul>
      )}
    </Card>
  )
}
