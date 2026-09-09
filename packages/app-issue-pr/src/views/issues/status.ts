import type { IssueStatus } from '../../api/types'
import type { BadgeTone } from '@orchestratord/ui'
import type { Locale } from '@orchestratord/app-contracts'

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
  return STATUS_LABELS[locale][status as IssueStatus] ?? status.replaceAll('_', ' ')
}

const STATUS_LABELS: Record<Locale, Record<IssueStatus, string>> = {
  en: { queued: 'Queued', pending: 'Pending', running: 'Running', pending_review: 'Pending review', completed: 'Completed', failed: 'Failed', abandoned: 'Abandoned', verification_failed: 'Verification failed' },
  'zh-CN': { queued: '排队中', pending: '待处理', running: '运行中', pending_review: '待评审', completed: '已完成', failed: '失败', abandoned: '已放弃', verification_failed: '验证失败' },
  ja: { queued: 'キュー待ち', pending: '未処理', running: '実行中', pending_review: 'レビュー待ち', completed: '完了', failed: '失敗', abandoned: '中止', verification_failed: '検証失敗' },
}
