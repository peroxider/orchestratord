import type { PullRequestState } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** §6.5 PR lifecycle coloring: open → merged → closed. */
export const PR_STATE_TONE: Record<PullRequestState, BadgeTone> = {
  open: 'good',
  merged: 'purple',
  closed: 'neutral',
}

export function prStateLabel(state: string, locale: Locale = 'en'): string {
  return translate(locale, `vcs.pr_state.${state}` as TranslationKey, state)
}

export function prStateTone(state: string): BadgeTone {
  return PR_STATE_TONE[state as PullRequestState] ?? 'neutral'
}
