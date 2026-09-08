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
import { UsageCharts } from './usage-charts'

export interface UsagePageProps {
  client: ApiClient
  workspaceId: string
}

export function UsagePage({ client, workspaceId }: UsagePageProps) {
  const [dimension, setDimension] = useState<UsageDimension>('agent')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const { locale } = useTranslation()
  const c = usageCopy[locale]

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
    a.download = `orchestratord-usage-${dimension}.csv`
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
            aria-label={c.from}
          />
          <input
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            aria-label={c.to}
          />
          <Button size="sm" variant="secondary" onClick={exportCsv}>
            {c.export}
          </Button>
        </div>
      </div>

      {isPending ? (
        <p className="usage-page__empty">{c.loading}</p>
      ) : isError ? (
        <p className="usage-page__empty">
          {c.failed}: {error?.message ?? c.unknown}
        </p>
      ) : (
        <>
          <div className="usage-page__totals">
            <Stat label={c.tokens} value={formatTokens(data?.totals.tokens_total ?? 0)} />
            <Stat label={c.cost} value={(data?.totals.cost_usd ?? 0) > 0 ? formatUsd(data!.totals.cost_usd) : c.noPricing} />
            <Stat label={c.sessions} value={formatTokens(data?.totals.sessions ?? 0)} />
          </div>

          <UsageCharts groups={data?.groups ?? []} dimension={dimension} pricingConfigured={(data?.totals.cost_usd ?? 0) > 0} />

          {data && data.groups.length === 0 ? (
            <p className="usage-page__empty">{c.empty}</p>
          ) : (
            <Card className="usage-page__table-card">
              <table className="usage-table">
                <thead>
                  <tr>
                    <th>{dimensionLabel(dimension, locale)}</th>
                    <th>{c.input}</th>
                    <th>{c.output}</th>
                    <th>{c.total}</th>
                    <th>{c.cost}</th>
                    <th>{c.sessions}</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.groups ?? []).map((g) => (
                    <tr key={g.group}>
                      <td>{g.group}</td>
                      <td>{formatTokens(g.tokens_in)}</td>
                      <td>{formatTokens(g.tokens_out)}</td>
                      <td>{formatTokens(g.tokens_total)}</td>
                      <td>{g.cost_usd > 0 ? formatUsd(g.cost_usd) : c.noPricing}</td>
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

const usageCopy = {
  en: { from: 'From', to: 'To', export: 'Export CSV', loading: 'Loading usage…', failed: 'Could not load usage', unknown: 'unknown error', tokens: 'Total tokens', cost: 'Cost', sessions: 'Sessions', noPricing: 'Pricing not configured', empty: 'No usage recorded.', input: 'Input', output: 'Output', total: 'Total' },
  'zh-CN': { from: '开始日期', to: '结束日期', export: '导出 CSV', loading: '正在加载用量…', failed: '无法加载用量', unknown: '未知错误', tokens: 'Token 总量', cost: '成本', sessions: '会话', noPricing: '尚未配置价格', empty: '暂无用量记录。', input: '输入', output: '输出', total: '总计' },
  ja: { from: '開始日', to: '終了日', export: 'CSV を出力', loading: '使用量を読み込み中…', failed: '使用量を読み込めませんでした', unknown: '不明なエラー', tokens: '総トークン', cost: 'コスト', sessions: 'セッション', noPricing: '価格未設定', empty: '使用量の記録はありません。', input: '入力', output: '出力', total: '合計' },
} as const

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
