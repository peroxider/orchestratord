'use client'

import { useSkills, useVerifySkill } from '@orchestratord/core'
import type { ApiClient, SkillSummary } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useLocale } from '../i18n'

export interface SkillsListProps {
  client: ApiClient
  workspaceId: string
}

export function SkillsList({ client, workspaceId }: SkillsListProps) {
  const { data, isPending, isError, error } = useSkills(client)
  const locale = useLocale()
  const c = skillCopy[locale]

  if (isPending) {
    return <p className="skills__empty">{c.loading}</p>
  }
  if (isError) {
    return (
      <p className="skills__empty">
        {c.failed}: {error?.message ?? 'unknown error'}
      </p>
    )
  }

  const skills = data ?? []
  if (skills.length === 0) {
    return <p className="skills__empty">{c.empty}</p>
  }

  return (
    <div className="skills">
      {skills.map((skill) => (
        <SkillCard
          key={skill.name}
          client={client}
          workspaceId={workspaceId}
          skill={skill}
          locale={locale}
        />
      ))}
    </div>
  )
}

function SkillCard({
  client,
  workspaceId,
  skill,
  locale,
}: {
  client: ApiClient
  workspaceId: string
  skill: SkillSummary
  locale: keyof typeof skillCopy
}) {
  const verify = useVerifySkill(client, skill.name)
  const c = skillCopy[locale]

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
          {skill.is_stale ? c.stale : c.verified}
        </Badge>
      </header>
      <p className="skill-card__description">{skill.description}</p>
      <Button
        size="sm"
        variant="secondary"
        disabled={verify.isPending}
        onClick={() => verify.mutate()}
      >
        {verify.isPending ? c.verifying : c.verify}
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

const skillCopy = {
  en: { loading: 'Loading skills…', failed: 'Failed to load skills', empty: 'No skills discovered.', stale: 'stale', verified: 'verified', verify: 'Verify', verifying: 'Verifying…' },
  'zh-CN': { loading: '正在加载技能…', failed: '无法加载技能', empty: '未发现技能。', stale: '已过期', verified: '已验证', verify: '验证', verifying: '正在验证…' },
  ja: { loading: 'スキルを読み込み中…', failed: 'スキルを読み込めませんでした', empty: 'スキルは見つかりませんでした。', stale: '要更新', verified: '検証済み', verify: '検証', verifying: '検証中…' },
} as const
