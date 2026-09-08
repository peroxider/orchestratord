import type { InboxItemStatus, InboxKind } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** §5.2.6 lifecycle coloring: open → assigned → resolved/dismissed. */
export const INBOX_STATUS_TONE: Record<InboxItemStatus, BadgeTone> = {
  open: 'warn',
  assigned: 'accent',
  resolved: 'good',
  dismissed: 'neutral',
}

export function inboxStatusLabel(
  status: InboxItemStatus,
  locale: Locale = 'en',
): string {
  return translate(locale, `inbox.status.${status}` as TranslationKey, status)
}

export function inboxKindLabel(kind: InboxKind, locale: Locale = 'en'): string {
  return translate(locale, `inbox.kind.${kind}` as TranslationKey, kind)
}
