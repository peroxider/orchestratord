'use client'

import { useState } from 'react'
import type { ApiClient } from '@orchestratord/core'
import type { FrontendApplication, Locale, OverviewWidgetProps } from '@orchestratord/app-contracts'
import { Button, Input, Textarea } from '@orchestratord/ui'
import { useLocale } from '@orchestratord/views'
import { useCreateIssue, useIssues } from './queries/issues'
import type { Issue } from './api/types'

const applicationName = { en: 'ISSUE → PR', 'zh-CN': '任务 → PR', ja: 'ISSUE → PR' } as const
const issuesLabel = { en: 'Issues', 'zh-CN': '任务', ja: 'Issue' } as const
const createLabel = { en: 'New issue', 'zh-CN': '新建任务', ja: 'Issue を作成' } as const

export function createIssuePrApplication(client: ApiClient): FrontendApplication {
  return {
    id: 'issue_pr',
    displayName: applicationName,
    icon: 'issues',
    order: 10,
    navigation: [{ id: 'issue_pr.navigation.issues', applicationId: 'issue_pr', label: issuesLabel, href: '/issues', icon: 'issues', order: 10, capability: 'issues.read' }],
    globalActions: [{
      id: 'issue_pr.action.create_issue', applicationId: 'issue_pr', label: createLabel, icon: 'plus', order: 10, capability: 'issues.write', shortcut: 'C I',
      render: context => <CreateIssueDialog client={client} workspaceId={context.workspaceId} navigate={context.navigate} close={context.close} />,
    }],
    searchProviders: [{
      id: 'issue_pr.search', applicationId: 'issue_pr', kinds: ['issue'],
      search: async (query, context) => {
        const issues = await client.request<Issue[]>(`/api/workspaces/${context.workspaceId}/issues`, { signal: context.signal })
        const needle = query.trim().toLocaleLowerCase()
        return issues.filter(issue => !needle || `${issue.title} ${issue.id}`.toLocaleLowerCase().includes(needle)).slice(0, 30).map(issue => ({ id: issue.id, title: issue.title, subtitle: issue.status.replaceAll('_', ' '), resource: { application_id: 'issue_pr', kind: 'issue', id: issue.id, label: issue.title } }))
      },
    }],
    resourcePresenters: [{
      id: 'issue_pr.resource.issue', applicationId: 'issue_pr', kind: 'issue',
      present: (ref, locale) => ({ label: ref.label || `${issuesLabel[locale]} · ${ref.id}`, href: `/issues/${encodeURIComponent(ref.id)}`, available: true, applicationName: applicationName[locale] }),
    }],
    overviewWidgets: [{ id: 'issue_pr.overview.recent', applicationId: 'issue_pr', order: 10, span: 2, component: props => <RecentIssues client={client} {...props} /> }],
    realtime: [{ id: 'issue_pr.realtime', applicationId: 'issue_pr', match: topic => topic.startsWith('issue.'), invalidations: (_topic, workspaceId) => [['application', 'issue_pr', 'issues', workspaceId]] }],
    autopilotTargets: [{ id: 'issue_pr.autopilot.issue', applicationId: 'issue_pr', kind: 'issue', label: issuesLabel, order: 10, capability: 'issues.read' }],
    onboarding: [{ id: 'issue_pr.onboarding.create', applicationId: 'issue_pr', order: 10, title: { en: 'Create the first issue', 'zh-CN': '创建首个任务', ja: '最初の Issue を作成' }, description: { en: 'Describe an outcome and start an auditable execution.', 'zh-CN': '描述目标并启动可审计的执行。', ja: '目標を記述し、監査可能な実行を開始します。' }, actionLabel: { en: 'Open issues', 'zh-CN': '打开任务', ja: 'Issue を開く' }, href: '/issues' }],
  }
}

function RecentIssues({ client, workspaceId, locale, navigate }: OverviewWidgetProps & { client: ApiClient }) {
  const issues = useIssues(client, workspaceId)
  const copy = {
    en: { eyebrow: 'ISSUE → PR / RECENT', title: 'Latest issues', action: 'Open issues →', issue: 'Issue', status: 'Status', created: 'Created', empty: 'No issues yet.' },
    'zh-CN': { eyebrow: '任务 → PR / 最近工作', title: '最新任务', action: '打开任务 →', issue: '任务', status: '状态', created: '创建时间', empty: '暂无任务。' },
    ja: { eyebrow: 'ISSUE → PR / 最近の作業', title: '最新の Issue', action: 'Issue を開く →', issue: 'Issue', status: '状態', created: '作成日時', empty: 'Issue はまだありません。' },
  }[locale]
  return <section className="ledger-panel overview-work application-widget" data-span="2"><header className="section-header"><div><p className="section-eyebrow">{copy.eyebrow}</p><h3>{copy.title}</h3></div><button className="text-action" onClick={() => navigate('/issues')}>{copy.action}</button></header>{issues.isPending ? <div className="widget-state">…</div> : issues.isError ? <div className="widget-state widget-state--error">{String(issues.error)}</div> : (issues.data ?? []).length === 0 ? <div className="widget-state">{copy.empty}</div> : <div className="work-table"><div className="work-table__head"><span>{copy.issue}</span><span>{copy.status}</span><span>{copy.created}</span></div>{(issues.data ?? []).slice(0, 5).map(issue => <button key={issue.id} onClick={() => navigate(`/issues/${issue.id}`)}><span><i>{issue.id.slice(0, 4).toUpperCase()}</i>{issue.title}</span><span data-status={issue.status}>{issue.status.replaceAll('_', ' ')}</span><time>{new Date(issue.created_at).toLocaleDateString(locale)}</time></button>)}</div>}</section>
}

function CreateIssueDialog({ client, workspaceId, navigate, close }: { client: ApiClient; workspaceId: string; navigate(href: string): void; close(): void }) {
  const locale = useLocale() as Locale
  const c = {
    en: { eyebrow: 'ISSUE → PR / NEW', title: 'New issue', titleLabel: 'Issue title', description: 'Describe the desired outcome…', cancel: 'Cancel', create: 'Create issue', creating: 'Creating…', error: 'Could not create the issue. Your draft is still here; check the local API and try again.', close: 'Close' },
    'zh-CN': { eyebrow: '任务 → PR / 新建', title: '新建任务', titleLabel: '任务标题', description: '描述期望结果…', cancel: '取消', create: '创建任务', creating: '正在创建…', error: '无法创建任务。草稿仍在此处；请检查本地 API 后重试。', close: '关闭' },
    ja: { eyebrow: 'ISSUE → PR / 新規', title: 'Issue を作成', titleLabel: 'Issue タイトル', description: '期待する結果を説明…', cancel: 'キャンセル', create: 'Issue を作成', creating: '作成中…', error: 'Issue を作成できませんでした。下書きは保持されています。ローカル API を確認して再試行してください。', close: '閉じる' },
  }[locale]
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const create = useCreateIssue(client, workspaceId)
  return <div className="dialog-layer" onMouseDown={event => { if (event.target === event.currentTarget) close() }}><form className="create-dialog" role="dialog" aria-modal="true" aria-label={c.title} onSubmit={event => { event.preventDefault(); if (!title.trim()) return; create.mutate({ title: title.trim(), description }, { onSuccess: issue => { close(); navigate(`/issues/${issue.id}`) } }) }}><header><div><span className="dialog-eyebrow">{c.eyebrow}</span><h2>{c.title}</h2></div><button type="button" className="icon-button" aria-label={c.close} onClick={close}>×</button></header><label>{c.titleLabel}<Input autoFocus value={title} onChange={event => setTitle(event.target.value)} /></label><label>{c.description}<Textarea value={description} onChange={event => setDescription(event.target.value)} placeholder={c.description} /></label>{create.isError && <p className="form-error">{c.error}</p>}<footer><Button type="button" variant="ghost" onClick={close}>{c.cancel}</Button><Button type="submit" disabled={!title.trim() || create.isPending}>{create.isPending ? c.creating : c.create}</Button></footer></form></div>
}
