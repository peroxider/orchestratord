import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/**
 * Canonical capability bits, in `BackendCapabilities` declaration order.
 * §5.2.2 requires the matrix to faithfully reflect the cached JSONB bits,
 * so unknown keys are preserved rather than dropped (see CapabilityMatrix).
 */
export const CAPABILITY_KEYS = [
  'streaming_deltas',
  'resumable',
  'interrupt',
  'approval_hooks',
  'parallel_sessions',
  'cost_reporting',
  'tool_filtering',
  'takeover',
  'goal_mode',
  'resume_detection',
] as const

export function capabilityLabel(
  key: string,
  locale: Locale = 'en',
): string {
  return translate(locale, `agents.capability.${key}` as TranslationKey, key)
}

export function capabilityLabels(
  locale: Locale = 'en',
): Array<{ key: string; label: string }> {
  return CAPABILITY_KEYS.map((key) => ({ key, label: capabilityLabel(key, locale) }))
}
