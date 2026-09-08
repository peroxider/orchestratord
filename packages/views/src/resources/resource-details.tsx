'use client'

import {
  useAgent,
  useAgents,
  useAutopilot,
  usePatchAutopilot,
  useProject,
  useRuntime,
  useSessions,
  useSquad,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { useLocale } from '../i18n'
import { cronSummary } from '../autopilots/schedule'
import { RUNTIME_STATUS_TONE, runtimeStatusLabel } from '../runtimes/runtime-status'

type BaseProps = { client: ApiClient; workspaceId: string }

function State({ loading, error, missing }: { loading: boolean; error?: Error | null; missing?: boolean }) {
  const c = copy[useLocale()]
  if (loading) return <p className="resource-detail__state">{c.loading}</p>
  if (error) return <p className="resource-detail__state">{c.failed}: {error.message}</p>
  if (missing) return <p className="resource-detail__state">{c.missing}</p>
  return null
}

function Crumb({ href, label, current }: { href: string; label: string; current: string }) {
  return <nav className="detail-breadcrumb" aria-label="Breadcrumb"><a href={href}>{label}</a><span>/</span><span aria-current="page">{current}</span></nav>
}

export function AgentDetail({ client, workspaceId, agentId }: BaseProps & { agentId: string }) {
  const locale = useLocale(); const c = copy[locale]
  const agent = useAgent(client, workspaceId, agentId)
  const runtimes = useRuntimeName(client, workspaceId, agent.data?.runtime_id)
  const sessions = useSessions(client, workspaceId)
  if (agent.isPending || agent.isError || !agent.data) return <State loading={agent.isPending} error={agent.error} missing={!agent.isPending && !agent.isError} />
  const recent = (sessions.data ?? []).filter(s => s.agent_id === agentId).slice(0, 6)
  return <div className="resource-detail"><Crumb href="/agents" label={c.agents} current={agent.data.name} /><header className="resource-detail__header"><div><p className="section-eyebrow">AGENT / EXECUTION</p><h2>{agent.data.name}</h2></div><Badge tone="accent">{agent.data.provider}</Badge></header><div className="resource-detail__grid"><Card><h3>{c.configuration}</h3><dl className="resource-facts"><div><dt>{c.backend}</dt><dd>{agent.data.provider}</dd></div><div><dt>{c.runtime}</dt><dd>{runtimes ?? c.unavailable}</dd></div><div><dt>{c.created}</dt><dd>{new Date(agent.data.created_at).toLocaleString(locale)}</dd></div></dl></Card><Card><h3>{c.capabilities}</h3><div className="resource-detail__badges">{Object.entries(agent.data.capabilities_cache_jsonb).map(([name, enabled]) => <Badge key={name} tone={enabled ? 'good' : 'neutral'}>{name} · {enabled ? c.available : c.unavailable}</Badge>)}</div></Card></div><RecentSessions sessions={recent} locale={locale} /></div>
}

function useRuntimeName(client: ApiClient, workspaceId: string, runtimeId?: string) {
  const runtime = useRuntime(client, workspaceId, runtimeId ?? '')
  return runtimeId ? runtime.data?.hostname : undefined
}

export function ProjectDetail({ client, workspaceId, projectId }: BaseProps & { projectId: string }) {
  const locale = useLocale(); const c = copy[locale]; const project = useProject(client, workspaceId, projectId)
  if (project.isPending || project.isError || !project.data) return <State loading={project.isPending} error={project.error} missing={!project.isPending && !project.isError} />
  const data = project.data
  return <div className="resource-detail"><Crumb href="/projects" label={c.projects} current={data.name} /><header className="resource-detail__header"><div><p className="section-eyebrow">PROJECT / CONTEXT</p><h2>{data.name}</h2><p>{data.description || c.noDescription}</p></div><Badge tone="good">{c.active}</Badge></header><div className="resource-detail__grid"><Card><h3>{c.repositories}</h3>{data.repos.length ? <ul className="resource-list">{data.repos.map(repo => <li key={repo.repo_url}><a href={repo.repo_url}>{repo.repo_url}</a><small>{repo.default_branch}</small></li>)}</ul> : <p>{c.none}</p>}</Card><Card><h3>{c.documents}</h3>{data.docs.length ? <ul className="resource-list">{data.docs.map(doc => <li key={doc.doc_url}><a href={doc.doc_url}>{doc.doc_url}</a><small>{doc.doc_type}</small></li>)}</ul> : <p>{c.none}</p>}</Card></div></div>
}

export function SquadDetail({ client, workspaceId, squadId }: BaseProps & { squadId: string }) {
  const locale = useLocale(); const c = copy[locale]; const squad = useSquad(client, workspaceId, squadId); const agents = useAgents(client, workspaceId)
  if (squad.isPending || squad.isError || !squad.data) return <State loading={squad.isPending} error={squad.error} missing={!squad.isPending && !squad.isError} />
  const names = new Map((agents.data ?? []).map(a => [a.id, a.name])); const data = squad.data
  return <div className="resource-detail"><Crumb href="/squads" label={c.squads} current={data.name} /><header className="resource-detail__header"><div><p className="section-eyebrow">SQUAD / ORCHESTRATION</p><h2>{data.name}</h2></div><Badge tone="purple">{c.agentSquad}</Badge></header><div className="resource-detail__grid"><Card><h3>{c.coordinator}</h3><p className="resource-detail__prominent">{names.get(data.leader_id) ?? data.leader_id.slice(0, 8)}</p></Card><Card><h3>{c.agents}</h3><ul className="resource-list">{data.members.filter(m => m.member_type === 'agent').map(m => <li key={m.member_id}><a href={`/agents/${m.member_id}`}>{names.get(m.member_id) ?? m.member_id.slice(0, 8)}</a></li>)}</ul></Card></div></div>
}

export function AutopilotDetail({ client, workspaceId, autopilotId }: BaseProps & { autopilotId: string }) {
  const locale = useLocale(); const c = copy[locale]; const autopilot = useAutopilot(client, workspaceId, autopilotId); const patch = usePatchAutopilot(client, workspaceId, autopilotId)
  if (autopilot.isPending || autopilot.isError || !autopilot.data) return <State loading={autopilot.isPending} error={autopilot.error} missing={!autopilot.isPending && !autopilot.isError} />
  const data = autopilot.data
  return <div className="resource-detail"><Crumb href="/autopilots" label={c.autopilots} current={data.name} /><header className="resource-detail__header"><div><p className="section-eyebrow">AUTOPILOT / SCHEDULE</p><h2>{data.name}</h2></div><Button size="sm" variant={data.enabled ? 'secondary' : 'primary'} disabled={patch.isPending} onClick={() => patch.mutate({ enabled: !data.enabled })}>{data.enabled ? c.pause : c.enable}</Button></header><div className="resource-detail__grid"><Card><h3>{c.schedule}</h3><p className="resource-detail__prominent">{cronSummary(data.cron, locale)}</p><code>{data.cron}</code><p>{c.nextUnavailable}</p></Card><Card><h3>{c.target}</h3><dl className="resource-facts"><div><dt>{c.kind}</dt><dd>{data.target_kind}</dd></div><div><dt>ID</dt><dd>{data.target_id}</dd></div></dl><p>{data.prompt}</p></Card></div><Card><h3>{c.history}</h3>{data.runs?.length ? <ul className="resource-list">{data.runs.map(run => <li key={`${run.run_id}-${run.scheduled_at}`}><span>{new Date(run.scheduled_at).toLocaleString(locale)}</span><Badge tone={run.status === 'completed' ? 'good' : run.status === 'failed' ? 'bad' : 'neutral'}>{run.status}</Badge></li>)}</ul> : <p>{c.noRuns}</p>}</Card></div>
}

export function RuntimeDetail({ client, workspaceId, runtimeId }: BaseProps & { runtimeId: string }) {
  const locale = useLocale(); const c = copy[locale]; const runtime = useRuntime(client, workspaceId, runtimeId); const agents = useAgents(client, workspaceId)
  if (runtime.isPending || runtime.isError || !runtime.data) return <State loading={runtime.isPending} error={runtime.error} missing={!runtime.isPending && !runtime.isError} />
  const data = runtime.data; const attached = (agents.data ?? []).filter(a => a.runtime_id === runtimeId)
  return <div className="resource-detail"><Crumb href="/runtimes" label={c.runtimes} current={data.hostname} /><header className="resource-detail__header"><div><p className="section-eyebrow">RUNTIME / NODE</p><h2>{data.hostname}</h2></div><Badge tone={RUNTIME_STATUS_TONE[data.status] ?? 'neutral'}>{runtimeStatusLabel(data.status, locale)}</Badge></header><div className="resource-detail__grid"><Card><h3>{c.health}</h3><dl className="resource-facts"><div><dt>OS</dt><dd>{data.os}</dd></div><div><dt>{c.lastHeartbeat}</dt><dd>{data.last_seen_at ? new Date(data.last_seen_at).toLocaleString(locale) : c.never}</dd></div><div><dt>{c.created}</dt><dd>{data.created_at ? new Date(data.created_at).toLocaleString(locale) : '—'}</dd></div></dl></Card><Card><h3>{c.backends}</h3><div className="resource-detail__badges">{data.probed_backends.length ? data.probed_backends.map(b => <Badge key={b.name} tone="accent">{b.name}{b.version ? ` · ${b.version}` : ''}</Badge>) : <p>{c.none}</p>}</div></Card></div><Card><h3>{c.attachedAgents}</h3>{attached.length ? <ul className="resource-list">{attached.map(a => <li key={a.id}><a href={`/agents/${a.id}`}>{a.name}</a><small>{a.provider}</small></li>)}</ul> : <p>{c.none}</p>}</Card></div>
}

function RecentSessions({ sessions, locale }: { sessions: Array<{ id: string; mode: string; status: string; created_at: string }>; locale: keyof typeof copy }) {
  const c = copy[locale]
  return <Card><h3>{c.recentSessions}</h3>{sessions.length ? <ul className="resource-list">{sessions.map(s => <li key={s.id}><a href={`/sessions/${s.id}`}>{s.mode} · {s.id.slice(0, 8)}</a><span><Badge tone="neutral">{s.status}</Badge> <small>{new Date(s.created_at).toLocaleString(locale)}</small></span></li>)}</ul> : <p>{c.none}</p>}</Card>
}

const copy = {
  en: { loading: 'Loading details…', failed: 'Could not load details', missing: 'This resource no longer exists.', agents: 'Agents', projects: 'Projects', squads: 'Squads', autopilots: 'Autopilots', runtimes: 'Runtimes', configuration: 'Configuration', backend: 'Backend', runtime: 'Runtime', unavailable: 'Unavailable', created: 'Created', capabilities: 'Capabilities', available: 'Available', recentSessions: 'Recent sessions', noDescription: 'No description.', active: 'Active', repositories: 'Repositories', documents: 'Documents', none: 'No data reported.', coordinator: 'Coordinator', agentSquad: 'Agent squad', schedule: 'Schedule', pause: 'Pause', enable: 'Enable', nextUnavailable: 'Next run will appear when the scheduler reports it.', target: 'Target', kind: 'Kind', history: 'Execution history', noRuns: 'No runs recorded.', health: 'Health', lastHeartbeat: 'Last heartbeat', never: 'Never reported', backends: 'Available backends', attachedAgents: 'Attached agents' },
  'zh-CN': { loading: '正在加载详情…', failed: '无法加载详情', missing: '此资源已不存在。', agents: 'Agent', projects: '项目', squads: 'Agent 小组', autopilots: '自动任务', runtimes: '运行时', configuration: '配置', backend: '后端', runtime: '运行时', unavailable: '不可用', created: '创建时间', capabilities: '能力', available: '可用', recentSessions: '最近会话', noDescription: '暂无描述。', active: '活跃', repositories: '代码仓库', documents: '文档', none: '暂无上报数据。', coordinator: '协调 Agent', agentSquad: 'Agent 小组', schedule: '计划', pause: '暂停', enable: '启用', nextUnavailable: '调度器上报后将在此显示下次运行时间。', target: '目标', kind: '类型', history: '执行历史', noRuns: '暂无运行记录。', health: '健康状态', lastHeartbeat: '最近心跳', never: '从未上报', backends: '可用后端', attachedAgents: '已绑定 Agent' },
  ja: { loading: '詳細を読み込み中…', failed: '詳細を読み込めませんでした', missing: 'このリソースは存在しません。', agents: 'エージェント', projects: 'プロジェクト', squads: 'Squad', autopilots: '自動タスク', runtimes: 'ランタイム', configuration: '設定', backend: 'バックエンド', runtime: 'ランタイム', unavailable: '利用不可', created: '作成日時', capabilities: '機能', available: '利用可能', recentSessions: '最近のセッション', noDescription: '説明はありません。', active: 'アクティブ', repositories: 'リポジトリ', documents: 'ドキュメント', none: '報告データはありません。', coordinator: 'コーディネーター', agentSquad: 'エージェント Squad', schedule: 'スケジュール', pause: '一時停止', enable: '有効化', nextUnavailable: 'スケジューラーから報告されると次回実行を表示します。', target: '対象', kind: '種類', history: '実行履歴', noRuns: '実行記録はありません。', health: 'ヘルス', lastHeartbeat: '最終ハートビート', never: '未報告', backends: '利用可能なバックエンド', attachedAgents: '接続済みエージェント' },
} as const
