'use client'

import { useState } from 'react'
import {
  useWorkspaceUsage,
  type ApiClient,
  type UsageDimension,
} from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import {
  USAGE_DIMENSIONS,
  dimensionLabel,
  formatTokens,
  formatUsd,
  toUsageCsv,
} from './usage-labels'

export interface UsagePageProps {
  client: ApiClient
  workspaceId: string
}

export function UsagePage({ client, workspaceId }: UsagePageProps) {
  const [dimension, setDimension] = useState<UsageDimension>('agent')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const { locale } = useTranslation()

  const { data, isPending, isError, error } = useWorkspaceUsage(
    client,
    workspaceId,
    { group_by: dimension, from: from || undefined, to: to || undefined },
  )

  function exportCsv() {
    const csv = toUsageCsv(data?.groups ?? [])
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `usage-${workspaceId}-${dimension}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="usage-page">
      <div className="usage-page__toolbar">
        <div className="usage-page__dimensions">
          {USAGE_DIMENSIONS.map((dim) => (
            <Button
              key={dim}
              size="sm"
              variant={dim === dimension ? 'primary' : 'secondary'}
              onClick={() => setDimension(dim)}
            >
              {dimensionLabel(dim, locale)}
            </Button>
          ))}
        </div>
        <div className="usage-page__range">
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
          <Button size="sm" variant="secondary" onClick={exportCsv}>
            Export CSV
          </Button>
        </div>
      </div>

      {isPending ? (
        <p className="usage-page__empty">Loading usage…</p>
      ) : isError ? (
        <p className="usage-page__empty">
          Failed to load usage: {error?.message ?? 'unknown error'}
        </p>
      ) : (
        <>
          <div className="usage-page__totals">
            <Stat label="Total tokens" value={formatTokens(data?.totals.tokens_total ?? 0)} />
            <Stat label="Cost" value={formatUsd(data?.totals.cost_usd ?? 0)} />
            <Stat label="Sessions" value={formatTokens(data?.totals.sessions ?? 0)} />
          </div>

          {data && data.groups.length === 0 ? (
            <p className="usage-page__empty">No usage recorded.</p>
          ) : (
            <Card className="usage-page__table-card">
              <table className="usage-table">
                <thead>
                  <tr>
                    <th>{dimensionLabel(dimension, locale)}</th>
                    <th>In</th>
                    <th>Out</th>
                    <th>Total</th>
                    <th>Cost</th>
                    <th>Sessions</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.groups ?? []).map((g) => (
                    <tr key={g.group}>
                      <td>{g.group}</td>
                      <td>{formatTokens(g.tokens_in)}</td>
                      <td>{formatTokens(g.tokens_out)}</td>
                      <td>{formatTokens(g.tokens_total)}</td>
                      <td>{formatUsd(g.cost_usd)}</td>
                      <td>{formatTokens(g.sessions)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </>
      )}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Card className="usage-stat">
      <div className="usage-stat__label">
        <Badge tone="neutral">{label}</Badge>
      </div>
      <div className="usage-stat__value">{value}</div>
    </Card>
  )
}
