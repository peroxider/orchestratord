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
          aria-label="Actor type"
        >
          <option value="">All actors</option>
          <option value="member">{auditActorTypeLabel('member', locale)}</option>
          <option value="agent">{auditActorTypeLabel('agent', locale)}</option>
          <option value="system">{auditActorTypeLabel('system', locale)}</option>
        </select>
        <input
          type="text"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          placeholder="Action"
          aria-label="Action"
        />
        <input
          type="date"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          aria-label="From"
        />
        <input
          type="date"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          aria-label="To"
        />
      </div>

      {isPending ? (
        <p className="audit__empty">Loading audit log…</p>
      ) : isError ? (
        <p className="audit__empty">
          Failed to load audit log: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="audit__empty">No audit entries.</p>
      ) : (
        <Card className="audit__table-card">
          <table className="audit-table">
            <thead>
              <tr>
                <th>Actor</th>
                <th>Action</th>
                <th>Target</th>
                <th>Time</th>
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
                  <td>{entry.target_type}</td>
                  <td>{new Date(entry.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}
