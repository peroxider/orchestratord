'use client'

import { useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useSessions, type Session } from '@orchestratord/core'
import { useLocale } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { useInstanceContext } from './app-shell'
import { useApplicationRegistry } from './application-registry'

const copy = {
  en: { eyebrow: 'ORCHESTRATION / EXECUTIONS', title: 'Execution sessions', note: 'Live work, waiting decisions, and durable execution evidence in one ledger.', filters: ['All', 'Running', 'Waiting', 'Failed', 'Completed'], groups: ['Active', 'Waiting for input', 'Queued', 'Failed', 'Completed'], count: 'sessions', columns: ['Session', 'Source', 'Mode', 'Status', 'Started'], error: 'Sessions could not be loaded.', errorNote: 'Your data is safe. Check the local API, then retry.', retry: 'Retry', empty: 'No sessions yet', emptyNote: 'Start a workflow or direct conversation to create an auditable execution record.', direct: 'Direct chat' },
  'zh-CN': { eyebrow: '编排 / 执行', title: '执行会话', note: '在同一本账本中查看实时工作、待决事项和持久化执行证据。', filters: ['全部', '运行中', '等待中', '失败', '已完成'], groups: ['活跃', '等待输入', '排队中', '失败', '已完成'], count: '个会话', columns: ['会话', '来源', '模式', '状态', '开始时间'], error: '无法加载会话。', errorNote: '数据未受影响。请检查本地 API 后重试。', retry: '重试', empty: '暂无会话', emptyNote: '启动业务流或直接对话后，这里会生成可审计的执行记录。', direct: '直接对话' },
  ja: { eyebrow: 'オーケストレーション / 実行', title: '実行セッション', note: '進行中の作業、判断待ち、永続的な実行証跡を一つの台帳で確認します。', filters: ['すべて', '実行中', '待機中', '失敗', '完了'], groups: ['実行中', '入力待ち', 'キュー', '失敗', '完了'], count: 'セッション', columns: ['セッション', 'ソース', 'モード', '状態', '開始日時'], error: 'セッションを読み込めませんでした', errorNote: 'データは変更されていません。ローカル API を確認して再試行してください。', retry: '再試行', empty: 'セッションはまだありません', emptyNote: 'ワークフローまたは直接会話を開始すると、監査可能な実行記録が作成されます。', direct: '直接チャット' },
} as const

const GROUP_STATUSES = [
  ['running'], ['paused', 'waiting', 'pending_review'], ['pending', 'queued'],
  ['failed', 'stopped'], ['completed'],
] as const

export function SessionsList() {
  const { workspace_id } = useInstanceContext()
  const sessions = useSessions(apiClient, workspace_id)
  const locale = useLocale()
  const c = copy[locale]
  const registry = useApplicationRegistry()
  const router = useRouter()
  const [filter, setFilter] = useState('all')
  const data = useMemo(() => [...(sessions.data ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)), [sessions.data])
  if (sessions.isPending) return <div className="table-skeleton"><i/><i/><i/><i/></div>
  if (sessions.isError) return <div className="page-state page-state--error"><strong>{c.error}</strong><p>{c.errorNote}</p><button onClick={() => sessions.refetch()}>{c.retry}</button></div>
  const filterValues = ['all', 'running', 'waiting', 'failed', 'completed']
  return <div className="sessions-page"><header className="collection-intro"><div><p className="section-eyebrow">{c.eyebrow}</p><h2>{c.title}</h2></div><p>{c.note}</p></header><div className="session-toolbar"><div role="tablist" aria-label="Session status">{filterValues.map((value, index) => <button key={value} role="tab" aria-selected={filter === value} onClick={() => setFilter(value)}>{c.filters[index]}</button>)}</div><span>{data.length} {c.count}</span></div>{data.length === 0 ? <div className="page-state"><strong>{c.empty}</strong><p>{c.emptyNote}</p></div> : <div className="session-groups">{GROUP_STATUSES.map((statuses, index) => { const rows = data.filter(session => statuses.includes(session.status as never) && (filter === 'all' || filter === 'waiting' ? filter === 'all' || index === 1 : session.status === filter)); if (!rows.length) return null; return <section key={c.groups[index]} className="session-section"><header><h3>{c.groups[index]}</h3><span>{rows.length}</span></header><div className="session-table"><div className="session-table__head">{c.columns.map(column => <span key={column}>{column}</span>)}</div>{rows.map(row => <SessionRow key={row.id} session={row} open={() => router.push(`/sessions/${row.id}`)} sourceLabel={row.origin.kind === 'resource' ? registry.resolveResource(row.origin.source, locale).label : c.direct} locale={locale} />)}</div></section> })}</div>}</div>
}

function SessionRow({ session, open, sourceLabel, locale }: { session: Session; open: () => void; sourceLabel: string; locale: string }) {
  return <button className="session-row" onClick={open}><span className="session-row__id"><i/><code>{session.id.slice(0, 8)}</code></span><span>{sourceLabel}</span><span>{session.mode}</span><span className="status-label" data-status={session.status}>{session.status.replaceAll('_', ' ')}</span><time>{new Date(session.created_at).toLocaleString(locale, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time></button>
}
