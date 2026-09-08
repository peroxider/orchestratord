import type { UsageDimension, UsageGroup } from '@orchestratord/core'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** The §5.2.4 grouping dimensions, in selector order. */
export const USAGE_DIMENSIONS: UsageDimension[] = [
  'agent',
  'backend',
  'issue',
  'day',
  'workspace',
]

export function dimensionLabel(
  dim: UsageDimension,
  locale: Locale = 'en',
): string {
  return translate(locale, `usage.dimension.${dim}` as TranslationKey, dim)
}

export function formatTokens(n: number): string {
  return n.toLocaleString('en-US')
}

export function formatUsd(n: number): string {
  return `$${n.toFixed(2)}`
}

/** Serialize grouped usage into CSV (header + one row per group). */
export function toUsageCsv(groups: UsageGroup[]): string {
  const header = ['group', 'tokens_in', 'tokens_out', 'tokens_total', 'cost_usd', 'sessions']
  const rows = groups.map((g) =>
    [g.group, g.tokens_in, g.tokens_out, g.tokens_total, g.cost_usd, g.sessions].join(','),
  )
  return [header.join(','), ...rows].join('\n')
}
