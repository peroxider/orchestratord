'use client'

import { useInbox, useRuntimes, useSessions, useWorkspaceUsage } from '@orchestratord/core'
import { Button } from '@orchestratord/ui'
import { useRouter } from 'next/navigation'
import { useLocale } from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { useInstanceContext } from './app-shell'
import { useApplicationRegistry } from './application-registry'

const ACTIVE = new Set(['running', 'pending', 'queued', 'paused', 'waiting'])

const overviewCopy = {
  en: {
    live: 'MISSION CONTROL / LIVE', lead: 'See what is moving.', accent: 'Act on what is blocked.', note: 'A single operational view of agent work, runtime health, and decisions waiting for you.', attention: 'Needs attention', approvals: 'approvals', questions: 'questions', failed: 'failed runs', offline: 'offline runtimes', spine: 'EXECUTION SPINE', active: 'Active sessions', all: 'View all →', quiet: 'No active execution. The ledger is quiet.', issueExecution: 'Issue execution', direct: 'Direct conversation', runtime: 'RUNTIME HEALTH', control: 'Control plane', nodes: 'Runtime nodes', online: 'online', backends: 'Available backends', executions: 'Active executions', pulse: 'USAGE PULSE', window: 'Current window', tokens: 'Tokens', sessions: 'Sessions', cost: 'Cost', noPricing: 'Pricing not configured', recent: 'RECENT WORK', latest: 'Latest issues', openIssues: 'Open issues →', issue: 'Issue', status: 'Status', created: 'Created', first: 'FIRST RUN / 3 STEPS', prepare: 'Prepare your local control plane', runtimeStep: 'Connect a runtime', runtimeNote: 'Register the machine that will execute agent work.', openRuntimes: 'Open runtimes', agentStep: 'Configure an agent', agentNote: 'Verify a backend, runtime, and capability set.', openAgents: 'Open agents', issueStep: 'Create the first issue', issueNote: 'Describe an outcome and start an auditable execution.', openIssueList: 'Open issues', sessionSuffix: 'session', loading: 'Loading overview',
  },
  'zh-CN': {
    live: '任务控制 / 实时', lead: '看清正在推进的工作。', accent: '立即处理受阻事项。', note: '在一个操作视图中掌握 Agent 工作、运行时健康状态和待决事项。', attention: '需要关注', approvals: '项审批', questions: '个问题', failed: '次失败', offline: '个运行时离线', spine: '执行脊柱', active: '活跃会话', all: '查看全部 →', quiet: '当前没有活跃执行，账本处于安静状态。', issueExecution: '任务执行', direct: '直接对话', runtime: '运行时健康', control: '控制平面', nodes: '运行时节点', online: '在线', backends: '可用后端', executions: '活跃执行', pulse: '用量脉冲', window: '当前窗口', tokens: 'Token', sessions: '会话', cost: '成本', noPricing: '尚未配置价格', recent: '最近工作', latest: '最新任务', openIssues: '打开任务 →', issue: '任务', status: '状态', created: '创建时间', first: '首次使用 / 3 步', prepare: '准备本地控制平面', runtimeStep: '连接运行时', runtimeNote: '注册用于执行 Agent 工作的机器。', openRuntimes: '打开运行时', agentStep: '配置 Agent', agentNote: '确认后端、运行时和能力集。', openAgents: '打开 Agent', issueStep: '创建首个任务', issueNote: '描述目标并启动可审计的执行。', openIssueList: '打开任务', sessionSuffix: '会话', loading: '正在加载总览',
  },
  ja: {
    live: 'ミッション制御 / ライブ', lead: '進行中の作業を把握し、', accent: '停止要因に対応する。', note: 'エージェントの作業、ランタイムの状態、判断待ちの項目を一つの画面で確認できます。', attention: '要対応', approvals: '件の承認', questions: '件の質問', failed: '件の失敗', offline: '件のランタイム停止', spine: '実行スパイン', active: '進行中のセッション', all: 'すべて表示 →', quiet: '進行中の実行はありません。台帳は静かです。', issueExecution: 'Issue 実行', direct: '直接会話', runtime: 'ランタイム状態', control: 'コントロールプレーン', nodes: 'ランタイムノード', online: 'オンライン', backends: '利用可能なバックエンド', executions: '進行中の実行', pulse: '使用量パルス', window: '現在の期間', tokens: 'トークン', sessions: 'セッション', cost: 'コスト', noPricing: '価格未設定', recent: '最近の作業', latest: '最新の Issue', openIssues: 'Issue を開く →', issue: 'Issue', status: '状態', created: '作成日時', first: '初回設定 / 3 ステップ', prepare: 'ローカル制御環境を準備', runtimeStep: 'ランタイムを接続', runtimeNote: 'エージェント作業を実行するマシンを登録します。', openRuntimes: 'ランタイムを開く', agentStep: 'エージェントを設定', agentNote: 'バックエンド、ランタイム、機能セットを確認します。', openAgents: 'エージェントを開く', issueStep: '最初の Issue を作成', issueNote: '目標を記述し、監査可能な実行を開始します。', openIssueList: 'Issue を開く', sessionSuffix: 'セッション', loading: '概要を読み込み中',
  },
} as const

export function Overview() {
  const { workspace_id } = useInstanceContext()
  const router = useRouter()
  const locale = useLocale()
  const c = overviewCopy[locale]
  const registry = useApplicationRegistry()
  const sessions = useSessions(apiClient, workspace_id)
  const inbox = useInbox(apiClient, workspace_id)
  const runtimes = useRuntimes(apiClient, workspace_id)
  const usage = useWorkspaceUsage(apiClient, workspace_id, { group_by: 'day' })
  const loading = sessions.isPending || inbox.isPending || runtimes.isPending
  if (loading) return <OverviewSkeleton label={c.loading} />
  const sessionList = sessions.data ?? []
  const inboxList = inbox.data ?? []
  const runtimeList = runtimes.data ?? []
  const active = sessionList.filter(s => ACTIVE.has(s.status)).slice(0, 6)
  const openAttention = inboxList.filter(i => i.status === 'open' || i.status === 'assigned')
  const offline = runtimeList.filter(r => r.status !== 'online')
  const widgets = registry.overviewWidgets()
  const onboarding = registry.onboarding()

  return <div className="overview">
    <section className="overview-intro"><div><p className="section-eyebrow">{c.live}</p><h2>{c.lead}<br/><span>{c.accent}</span></h2></div><p className="overview-intro__note">{c.note}</p></section>
    {(openAttention.length > 0 || offline.length > 0) && <section className="attention-strip" aria-label={c.attention}><div className="attention-strip__lead"><span className="attention-beacon"/><strong>{c.attention}</strong></div><button onClick={() => router.push('/inbox')}><b>{openAttention.filter(i => i.kind === 'approval_request').length}</b><span>{c.approvals}</span></button><button onClick={() => router.push('/inbox')}><b>{openAttention.filter(i => i.kind === 'clarification').length}</b><span>{c.questions}</span></button><button onClick={() => router.push('/sessions')}><b>{sessionList.filter(s => s.status === 'failed').length}</b><span>{c.failed}</span></button><button onClick={() => router.push('/runtimes')}><b>{offline.length}</b><span>{c.offline}</span></button></section>}
    {sessionList.length === 0 && runtimeList.length === 0 ? <FirstRun router={router} copy={c} onboarding={onboarding} locale={locale} /> : <div className="overview-grid">
      <section className="ledger-panel overview-sessions"><header className="section-header"><div><p className="section-eyebrow">{c.spine}</p><h3>{c.active}</h3></div><button className="text-action" onClick={() => router.push('/sessions')}>{c.all}</button></header>{active.length === 0 ? <div className="quiet-empty"><span>○</span><p>{c.quiet}</p></div> : <ol className="execution-spine">{active.map((session, index) => { const source = session.origin.kind === 'resource' ? registry.resolveResource(session.origin.source, locale).label : c.direct; return <li key={session.id} className={`spine-node spine-node--${session.status}`}><div className="spine-node__marker"><span>{String(index + 1).padStart(2, '0')}</span></div><button onClick={() => router.push(`/sessions/${session.id}`)}><div><strong>{session.mode} {c.sessionSuffix}</strong><small>{source} · {new Date(session.created_at).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}</small></div><div className="spine-node__state"><i/>{session.status}</div></button></li> })}</ol>}</section>
      <aside className="overview-side"><section className="ledger-panel"><header className="section-header"><div><p className="section-eyebrow">{c.runtime}</p><h3>{c.control}</h3></div></header><dl className="health-list"><div><dt>{c.nodes}</dt><dd><b>{runtimeList.filter(r => r.status === 'online').length}</b> / {runtimeList.length} {c.online}</dd></div><div><dt>{c.backends}</dt><dd>{new Set(runtimeList.flatMap(r => r.probed_backends.map(b => b.name))).size}</dd></div><div><dt>{c.executions}</dt><dd>{active.length}</dd></div></dl></section><section className="ledger-panel"><header className="section-header"><div><p className="section-eyebrow">{c.pulse}</p><h3>{c.window}</h3></div></header><dl className="usage-pulse"><div><dt>{c.tokens}</dt><dd>{formatCompact(usage.data?.totals.tokens_total, locale)}</dd></div><div><dt>{c.sessions}</dt><dd>{usage.data?.totals.sessions ?? '—'}</dd></div><div><dt>{c.cost}</dt><dd>{usage.data && usage.data.totals.cost_usd > 0 ? `$${usage.data.totals.cost_usd.toFixed(2)}` : c.noPricing}</dd></div></dl></section></aside>
      {widgets.map(widget => { const Widget = widget.component; return <Widget key={widget.id} workspaceId={workspace_id} locale={locale} navigate={href => router.push(href)} /> })}
    </div>}
  </div>
}

function formatCompact(value: number | undefined, locale: string) { if (value == null) return '—'; return new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(value) }
function OverviewSkeleton({ label }: { label: string }) { return <div className="overview-skeleton" aria-label={label}><div/><div/><div/><div/></div> }
function FirstRun({ router, copy: c, onboarding, locale }: { router: ReturnType<typeof useRouter>; copy: typeof overviewCopy[keyof typeof overviewCopy]; onboarding: ReturnType<ReturnType<typeof useApplicationRegistry>['onboarding']>; locale: 'en' | 'zh-CN' | 'ja' }) { const steps = [{ title: c.runtimeStep, description: c.runtimeNote, action: c.openRuntimes, href: '/runtimes' }, { title: c.agentStep, description: c.agentNote, action: c.openAgents, href: '/agents' }, ...onboarding.map(item => ({ title: item.title[locale], description: item.description[locale], action: item.actionLabel[locale], href: item.href }))]; return <section className="first-run"><p className="section-eyebrow">{c.first}</p><h3>{c.prepare}</h3><div>{steps.map((step, index) => <article key={step.href}><b>{String(index + 1).padStart(2, '0')}</b><h4>{step.title}</h4><p>{step.description}</p><Button variant={index === steps.length - 1 ? 'primary' : 'secondary'} onClick={() => router.push(step.href)}>{step.action}</Button></article>)}</div></section> }
