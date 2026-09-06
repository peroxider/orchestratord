import type { MemberRole } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** §5.7.1 role matrix, in selector order. */
export const MEMBER_ROLES: MemberRole[] = ['owner', 'admin', 'member']

export const MEMBER_ROLE_TONE: Record<MemberRole, BadgeTone> = {
  owner: 'purple',
  admin: 'accent',
  member: 'neutral',
}

export function memberRoleLabel(role: MemberRole, locale: Locale = 'en'): string {
  return translate(locale, `members.role.${role}` as TranslationKey, role)
}
