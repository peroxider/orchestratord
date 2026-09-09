type PullRequestState = 'open' | 'closed' | 'merged'
import type { BadgeTone } from '@orchestratord/ui'
import type { Locale } from '@orchestratord/app-contracts'

/** §6.5 PR lifecycle coloring: open → merged → closed. */
export const PR_STATE_TONE: Record<PullRequestState, BadgeTone> = {
  open: 'good',
  merged: 'purple',
  closed: 'neutral',
}

export function prStateLabel(state: string, locale: Locale = 'en'): string {
  return ({ en: { open: 'Open', closed: 'Closed', merged: 'Merged' }, 'zh-CN': { open: '开放', closed: '已关闭', merged: '已合并' }, ja: { open: 'オープン', closed: 'クローズ', merged: 'マージ済み' } }[locale] as Record<string, string>)[state] ?? state
}

export function prStateTone(state: string): BadgeTone {
  return PR_STATE_TONE[state as PullRequestState] ?? 'neutral'
}
