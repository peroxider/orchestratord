'use client'

import type { UsageDimension, UsageGroup } from '@orchestratord/core'
import { Card } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'
import { dimensionLabel, formatTokens, formatUsd } from './usage-labels'

/** Metrics a chart can plot on the UsageGroup shape (§7.3). */
export type UsageMetric = 'tokens_total' | 'cost_usd' | 'sessions'

const METRIC_KEYS: Record<UsageMetric, TranslationKey> = {
  tokens_total: 'usage.metric.tokens_total',
  cost_usd: 'usage.metric.cost_usd',
  sessions: 'usage.metric.sessions',
}

function metricLabel(metric: UsageMetric, locale: Locale): string {
  return translate(locale, METRIC_KEYS[metric], metric)
}

function formatMetric(metric: UsageMetric, value: number): string {
  return metric === 'cost_usd' ? formatUsd(value) : formatTokens(value)
}

function chartTitle(metric: UsageMetric, dimension: UsageDimension, locale: Locale): string {
  return `${metricLabel(metric, locale)} · ${dimensionLabel(dimension, locale)}`
}

export interface UsageChartsProps {
  groups: UsageGroup[]
  dimension: UsageDimension
  pricingConfigured?: boolean
}

/** §7.3 — bar chart (tokens) + line chart (cost) over the grouped usage rows. */
export function UsageCharts({ groups, dimension, pricingConfigured = true }: UsageChartsProps) {
  const { locale } = useTranslation()
  if (groups.length === 0) return null
  return (
    <div className="usage-charts">
      <UsageBarChart groups={groups} metric="tokens_total" dimension={dimension} />
      {pricingConfigured ? <UsageLineChart groups={groups} metric="cost_usd" dimension={dimension} /> : <Card className="usage-chart usage-chart--empty"><h3 className="usage-chart__title">{metricLabel('cost_usd', locale)}</h3><p>{{ en: 'Pricing not configured. Token data remains available.', 'zh-CN': '尚未配置价格，Token 数据仍可使用。', ja: '価格未設定です。トークンデータは引き続き利用できます。' }[locale]}</p></Card>}
    </div>
  )
}

export interface UsageBarChartProps {
  groups: UsageGroup[]
  metric: UsageMetric
  dimension: UsageDimension
}

/** Horizontal bars — one per group, scaled to the group maximum. */
export function UsageBarChart({ groups, metric, dimension }: UsageBarChartProps) {
  const { locale } = useTranslation()
  if (groups.length === 0) return null
  const max = Math.max(...groups.map((g) => g[metric]), 0)
  const title = chartTitle(metric, dimension, locale)
  return (
    <Card className="usage-chart">
      <h3 className="usage-chart__title">{title}</h3>
      <div className="usage-chart__bars" role="img" aria-label={title}>
        {groups.map((g) => (
          <div key={g.group} className="usage-chart__row">
            <span className="usage-chart__label" title={g.group}>
              {g.group}
            </span>
            <span className="usage-chart__track">
              <span
                className="usage-chart__fill"
                style={{ width: `${max > 0 ? (g[metric] / max) * 100 : 0}%` }}
              />
            </span>
            <span className="usage-chart__value">{formatMetric(metric, g[metric])}</span>
          </div>
        ))}
      </div>
    </Card>
  )
}

export interface UsageLineChartProps {
  groups: UsageGroup[]
  metric: UsageMetric
  dimension: UsageDimension
}

const LINE_W = 320
const LINE_H = 120
const LINE_PAD = 6

/** SVG polyline across the groups (a time series for the `day` dimension). */
export function UsageLineChart({ groups, metric, dimension }: UsageLineChartProps) {
  const { locale } = useTranslation()
  if (groups.length === 0) return null
  const max = Math.max(...groups.map((g) => g[metric]), 0)
  const title = chartTitle(metric, dimension, locale)
  const stepX =
    groups.length > 1 ? (LINE_W - LINE_PAD * 2) / (groups.length - 1) : 0
  const points = groups.map((g, i) => ({
    group: g.group,
    value: g[metric],
    x: LINE_PAD + i * stepX,
    y:
      LINE_H -
      LINE_PAD -
      (max > 0 ? (g[metric] / max) * (LINE_H - LINE_PAD * 2) : 0),
  }))
  const polyline = points.map((p) => `${p.x},${p.y}`).join(' ')
  const area = `${LINE_PAD},${LINE_H - LINE_PAD} ${polyline} ${
    points[points.length - 1]?.x ?? LINE_PAD
  },${LINE_H - LINE_PAD}`
  return (
    <Card className="usage-chart">
      <h3 className="usage-chart__title">{title}</h3>
      <svg
        className="usage-chart__line"
        viewBox={`0 0 ${LINE_W} ${LINE_H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={title}
      >
        <polygon className="usage-chart__area" points={area} />
        <polyline className="usage-chart__polyline" points={polyline} />
        {points.map((p) => (
          <circle key={p.group} className="usage-chart__dot" cx={p.x} cy={p.y} r={2.5}>
            <title>{`${p.group}: ${formatMetric(metric, p.value)}`}</title>
          </circle>
        ))}
      </svg>
      <div className="usage-chart__axis">
        <span>{groups[0]?.group}</span>
        <span>{groups[groups.length - 1]?.group}</span>
      </div>
    </Card>
  )
}
