'use client'

import { useMemo, useState, type ComponentType } from 'react'
import {
  useDismissInbox,
  useInbox,
  useInboxDecision,
  useAnswerInboxClarification,
  useResolveInbox,
  type ApiClient,
  type InboxKind,
} from '@orchestratord/core'
import { ApprovalCard } from './approval-card'
import { ClarificationCard } from './clarification-card'
import { FailureCard } from './failure-card'
import type { InboxKindCardProps } from './inbox-shared'
import { useLocale } from '../i18n'

/** §7.4 — one differentiated card view per inbox kind (§7.5 acceptance). */
const KIND_CARDS: Record<InboxKind, ComponentType<InboxKindCardProps>> = {
  approval_request: ApprovalCard,
  clarification: ClarificationCard,
  failed: FailureCard,
}

export interface InboxListProps {
  client: ApiClient
  workspaceId: string
}

export function InboxList({ client, workspaceId }: InboxListProps) {
  const locale = useLocale()
  const c = FILTER_COPY[locale]
  const [kind, setKind] = useState<InboxKind | 'all'>('all')
  const [newestFirst, setNewestFirst] = useState(true)
  const { data, isPending, isError, error } = useInbox(client, workspaceId)
  const resolve = useResolveInbox(client, workspaceId)
  const dismiss = useDismissInbox(client, workspaceId)
  const decision = useInboxDecision(client, workspaceId)
  const answer = useAnswerInboxClarification(client, workspaceId)
  const items = useMemo(() => {
    const filtered = (data ?? []).filter(item => kind === 'all' || item.kind === kind)
    return [...filtered].sort((a, b) => {
      const order = b.created_at.localeCompare(a.created_at)
      return newestFirst ? order : -order
    })
  }, [data, kind, newestFirst])

  if (isPending) {
    return <p className="inbox__empty">{c.loading}</p>
  }
  if (isError) {
    return (
      <p className="inbox__empty">
        {c.failed}: {error?.message ?? c.unknown}
      </p>
    )
  }

  if (items.length === 0) {
    return <><InboxToolbar kind={kind} setKind={setKind} newestFirst={newestFirst} setNewestFirst={setNewestFirst} locale={locale} /><p className="inbox__empty">{c.empty}</p></>
  }

  return (
    <><InboxToolbar kind={kind} setKind={setKind} newestFirst={newestFirst} setNewestFirst={setNewestFirst} locale={locale} />{(decision.isError || answer.isError) && <p className="inbox__action-error" role="alert">{c.decisionFailed}: {(decision.error ?? answer.error)?.message}</p>}<ul className="inbox">
      {items.map((item) => {
        const KindCard = KIND_CARDS[item.kind] ?? FailureCard
        return (
          <KindCard
            key={item.id}
            item={item}
            workspaceId={workspaceId}
            busy={resolve.isPending || dismiss.isPending || decision.isPending || answer.isPending}
            onResolve={() => item.kind === 'approval_request' ? decision.mutate({ item, decision: 'approve' }) : resolve.mutate({ itemId: item.id })}
            onDismiss={() => item.kind === 'approval_request' ? decision.mutate({ item, decision: 'deny' }) : dismiss.mutate({ itemId: item.id })}
            onAnswer={(value) => answer.mutate({ itemId: item.id, answer: value })}
          />
        )
      })}
    </ul></>
  )
}

const FILTER_COPY = {
  en: { all: 'All', approval_request: 'Approvals', clarification: 'Questions', failed: 'Failed', newest: 'Newest first', oldest: 'Oldest first', loading: 'Loading inbox…', unknown: 'unknown error', empty: 'Nothing needs attention.', type: 'Inbox type', decisionFailed: 'The decision was not confirmed; this item remains open' },
  'zh-CN': { all: '全部', approval_request: '审批', clarification: '问题', failed: '失败', newest: '最新优先', oldest: '最早优先', loading: '正在加载收件箱…', unknown: '未知错误', empty: '目前没有需要处理的事项。', type: '收件箱类型', decisionFailed: '决定尚未确认，此事项仍保持待处理' },
  ja: { all: 'すべて', approval_request: '承認', clarification: '質問', failed: '失敗', newest: '新しい順', oldest: '古い順', loading: '受信箱を読み込み中…', unknown: '不明なエラー', empty: '対応が必要な項目はありません。', type: '受信箱タイプ', decisionFailed: '判断を確認できなかったため、この項目は未処理のままです' },
} as const

function InboxToolbar({ kind, setKind, newestFirst, setNewestFirst, locale }: { kind: InboxKind | 'all'; setKind: (kind: InboxKind | 'all') => void; newestFirst: boolean; setNewestFirst: (value: boolean) => void; locale: keyof typeof FILTER_COPY }) {
  const c = FILTER_COPY[locale]
  const kinds: Array<InboxKind | 'all'> = ['all', 'approval_request', 'clarification', 'failed']
  return <div className="inbox-toolbar"><div role="tablist" aria-label={c.type}>{kinds.map(value => <button key={value} role="tab" aria-selected={kind === value} onClick={() => setKind(value)}>{c[value]}</button>)}</div><button className="inbox-toolbar__sort" onClick={() => setNewestFirst(!newestFirst)}>{newestFirst ? c.newest : c.oldest}</button></div>
}
