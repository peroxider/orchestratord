'use client'

import { useSkill, useVerifySkill } from '@orchestratord/core'
import type { ApiClient } from '@orchestratord/core'
import { Badge, Button } from '@orchestratord/ui'
import { useLocale } from '../i18n'

export interface SkillDetailProps {
  client: ApiClient
  name: string
}

export function SkillDetail({ client, name }: SkillDetailProps) {
  const skill = useSkill(client, name)
  const verify = useVerifySkill(client, name)
  const locale = useLocale()
  const c = {
    en: { loading: 'Loading skill…', failed: 'Failed to load skill', stale: 'stale', verified: 'verified', verify: 'Verify', source: 'Source map', breadcrumb: 'Skills' },
    'zh-CN': { loading: '正在加载技能…', failed: '无法加载技能', stale: '已过期', verified: '已验证', verify: '验证', source: '来源映射', breadcrumb: '技能' },
    ja: { loading: 'スキルを読み込み中…', failed: 'スキルを読み込めませんでした', stale: '要更新', verified: '検証済み', verify: '検証', source: 'ソースマップ', breadcrumb: 'スキル' },
  }[locale]

  if (skill.isPending) {
    return <p className="skill-detail__empty">{c.loading}</p>
  }
  if (skill.isError) {
    return (
      <p className="skill-detail__empty">
        {c.failed}: {skill.error?.message ?? 'unknown error'}
      </p>
    )
  }

  const data = skill.data

  return (
    <div className="skill-detail">
      <nav className="detail-breadcrumb" aria-label="Breadcrumb"><a href="/skills">{c.breadcrumb}</a><span>/</span><span aria-current="page">{data.display_name}</span></nav>
      <header className="skill-detail__header">
        <h2 className="skill-detail__title">{data.display_name}</h2>
        <Badge tone={data.is_stale ? 'warn' : 'good'}>
          {data.is_stale ? c.stale : c.verified}
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
          {c.verify}
        </Button>
        {verify.data && (
          <Badge tone={verify.data.verified ? 'good' : 'warn'}>
            {verify.data.verified ? c.verified : c.stale}
          </Badge>
        )}
      </div>

      <section className="skill-detail__source-map">
        <h3>{c.source}</h3>
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
