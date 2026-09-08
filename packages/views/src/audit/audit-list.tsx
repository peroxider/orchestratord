'use client'

import { useState } from 'react'
import {
  useAudit,
  type ApiClient,
  type AuditActorType,
} from '@orchestratord/core'
import { Badge, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { auditActorTypeLabel } from './audit-labels'

export interface AuditListProps {
  client: ApiClient
  workspaceId: string
}

export function AuditList({ client, workspaceId }: AuditListProps) {
  const [actorType, setActorType] = useState<AuditActorType | ''>('')
  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const { locale } = useTranslation()
  const c = activityCopy[locale]

  const { data, isPending, isError, error } = useAudit(client, workspaceId, {
    actor_type: actorType || undefined,
    action: action || undefined,
    from: from || undefined,
    to: to || undefined,
  })

  return (
    <div className="audit">
      <div className="audit__toolbar">
        <select
          value={actorType}
          onChange={(e) => setActorType(e.target.value as AuditActorType | '')}
          aria-label={c.actorType}
        >
          <option value="">{c.allActors}</option>
          <option value="member">{auditActorTypeLabel('member', locale)}</option>
          <option value="agent">{auditActorTypeLabel('agent', locale)}</option>
          <option value="system">{auditActorTypeLabel('system', locale)}</option>
        </select>
        <input
          type="text"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          placeholder={c.action}
          aria-label={c.action}
        />
        <input
          type="date"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          aria-label={c.from}
        />
        <input
          type="date"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          aria-label={c.to}
        />
      </div>

      {isPending ? (
        <p className="audit__empty">{c.loading}</p>
      ) : isError ? (
        <p className="audit__empty">
          {c.failed}: {error?.message ?? c.unknown}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="audit__empty">{c.empty}</p>
      ) : (
        <Card className="audit__table-card">
          <table className="audit-table">
            <thead>
              <tr>
                <th>{c.actor}</th>
                <th>{c.action}</th>
                <th>{c.target}</th>
                <th>{c.time}</th>
              </tr>
            </thead>
            <tbody>
              {(data ?? []).map((entry) => (
                <tr key={entry.id}>
                  <td>
                    <Badge tone="neutral">
                      {auditActorTypeLabel(entry.actor_type, locale)}
                    </Badge>
                  </td>
                  <td>{entry.action}</td>
                  <td>{activityTargetHref(entry.target_type, entry.target_id) ? <a href={activityTargetHref(entry.target_type, entry.target_id)!}>{entry.target_type} · {entry.target_id.slice(0, 8)}</a> : entry.target_type}</td>
                  <td>{new Date(entry.created_at).toLocaleString(locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}

function activityTargetHref(type: string, id: string): string | null {
  const collection = ({ issue: 'issues', session: 'sessions', agent: 'agents', runtime: 'runtimes', project: 'projects', squad: 'squads', autopilot: 'autopilots' } as Record<string, string>)[type]
  return collection ? `/${collection}/${encodeURIComponent(id)}` : null
}

const activityCopy = {
  en: { actorType: 'Actor type', allActors: 'All actors', action: 'Action', from: 'From', to: 'To', loading: 'Loading activity…', failed: 'Could not load activity', unknown: 'unknown error', empty: 'No activity in this range.', actor: 'Actor', target: 'Target', time: 'Time' },
  'zh-CN': { actorType: '操作者类型', allActors: '全部操作者', action: '操作', from: '开始日期', to: '结束日期', loading: '正在加载活动…', failed: '无法加载活动', unknown: '未知错误', empty: '此范围内暂无活动。', actor: '操作者', target: '目标', time: '时间' },
  ja: { actorType: '実行者タイプ', allActors: 'すべての実行者', action: '操作', from: '開始日', to: '終了日', loading: 'アクティビティを読み込み中…', failed: 'アクティビティを読み込めませんでした', unknown: '不明なエラー', empty: 'この範囲にアクティビティはありません。', actor: '実行者', target: '対象', time: '時刻' },
} as const
