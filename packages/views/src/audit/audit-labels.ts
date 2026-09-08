import type { AuditActorType } from '@orchestratord/core'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

export function auditActorTypeLabel(
  actorType: AuditActorType,
  locale: Locale = 'en',
): string {
  return translate(locale, `audit.actor.${actorType}` as TranslationKey, actorType)
}
