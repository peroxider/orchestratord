import type { IssueStatus } from '@orchestratord/core'
import type { BadgeTone } from '@orchestratord/ui'
import { translate } from '../i18n/dictionaries'
import type { Locale, TranslationKey } from '../i18n/dictionaries'

/** The §5.2.1 display status set, in board column order. */
export const ISSUE_STATUSES: IssueStatus[] = [
  'queued',
  'pending',
  'running',
  'pending_review',
  'completed',
  'failed',
  'abandoned',
  'verification_failed',
]

export const STATUS_TONE: Record<IssueStatus, BadgeTone> = {
  queued: 'neutral',
  pending: 'warn',
  running: 'accent',
  pending_review: 'purple',
  completed: 'good',
  failed: 'bad',
  abandoned: 'neutral',
  verification_failed: 'bad',
}

export function issueStatusLabel(status: string, locale: Locale = 'en'): string {
  return translate(locale, `issues.status.${status}` as TranslationKey, status)
}
