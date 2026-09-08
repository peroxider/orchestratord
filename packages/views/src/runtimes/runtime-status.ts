import type { RuntimeStatus } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** §6.3 runtime lifecycle coloring: online → offline → disabled. */
export const RUNTIME_STATUS_TONE: Record<RuntimeStatus, BadgeTone> = {
  online: 'good',
  offline: 'warn',
  disabled: 'neutral',
}

export function runtimeStatusLabel(
  status: RuntimeStatus,
  locale: Locale = 'en',
): string {
  return translate(locale, `runtimes.status.${status}` as TranslationKey, status)
}
