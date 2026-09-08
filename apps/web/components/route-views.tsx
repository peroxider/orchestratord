'use client'

import type { ReactNode } from 'react'

import {
  AgentsList, AuditList, AutopilotsList, ChatPage, InboxList, IssueDetail,
  ProjectsList, RuntimesList, SessionDetail, SkillDetail, SkillsList,
  SquadsList, UsagePage,
  useLocale,
  type Locale,
} from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { useInstanceContext } from './app-shell'
import { IssuesBoard } from './issues-board'

type CollectionText = { eyebrow: string; title: string; description: string }
type LocalizedCollection = Record<Locale, CollectionText>

function Collection({ copy, children }: { copy: LocalizedCollection; children: ReactNode }) {
  const c = copy[useLocale()]
  return <div className="collection-page"><header className="collection-intro"><div><p className="section-eyebrow">{c.eyebrow}</p><h2>{c.title}</h2></div><p>{c.description}</p></header>{children}</div>
}

const C = (en: CollectionText, zh: CollectionText, ja: CollectionText): LocalizedCollection => ({ en, 'zh-CN': zh, ja })
const collections = {
  issues: C({ eyebrow: 'WORK / LEDGER', title: 'Issues', description: 'Create, sort, and advance work from intent to verified result.' }, { eyebrow: '工作 / 账本', title: '任务', description: '创建、整理任务，并从目标推进到可验证的结果。' }, { eyebrow: '作業 / 台帳', title: 'Issue', description: '作業を作成・整理し、意図から検証済みの結果まで進めます。' }),
  inbox: C({ eyebrow: 'ATTENTION / OPEN', title: 'Needs your decision', description: 'Approvals, questions, and failed work are collected here until resolved.' }, { eyebrow: '需要关注 / 待处理', title: '等待你的决定', description: '审批、问题和失败任务会集中在此，直至处理完成。' }, { eyebrow: '要対応 / 未処理', title: '判断が必要です', description: '承認、質問、失敗した作業を解決まで一か所に集めます。' }),
  chat: C({ eyebrow: 'ATTENTION / CONVERSATIONS', title: 'Agent conversations', description: 'Continue a task, inspect live responses, or open its execution record.' }, { eyebrow: '需要关注 / 对话', title: 'Agent 对话', description: '继续任务、检查实时回复，或打开对应执行记录。' }, { eyebrow: '要対応 / 会話', title: 'エージェントとの会話', description: '作業の継続、ライブ応答の確認、実行記録への移動ができます。' }),
  agents: C({ eyebrow: 'ORCHESTRATION / AGENTS', title: 'Execution agents', description: 'Backends, runtime bindings, and the capabilities each agent can exercise.' }, { eyebrow: '编排 / AGENT', title: '执行 Agent', description: '查看每个 Agent 的后端、运行时绑定与可用能力。' }, { eyebrow: 'オーケストレーション / エージェント', title: '実行エージェント', description: '各エージェントのバックエンド、ランタイム、利用可能な機能を確認します。' }),
  projects: C({ eyebrow: 'WORK / PROJECTS', title: 'Projects', description: 'Group related issues and execution context without permission boundaries.' }, { eyebrow: '工作 / 项目', title: '项目', description: '聚合同类任务与执行上下文，不引入权限边界。' }, { eyebrow: '作業 / プロジェクト', title: 'プロジェクト', description: '権限境界を設けず、関連する Issue と実行コンテキストをまとめます。' }),
  runtimes: C({ eyebrow: 'SYSTEM / RUNTIMES', title: 'Runtime nodes', description: 'Machines and backends available to execute agent work.' }, { eyebrow: '系统 / 运行时', title: '运行时节点', description: '用于执行 Agent 工作的机器与后端。' }, { eyebrow: 'システム / ランタイム', title: 'ランタイムノード', description: 'エージェント作業を実行できるマシンとバックエンドです。' }),
  skills: C({ eyebrow: 'SYSTEM / SKILLS', title: 'Skill catalog', description: 'Verified instructions and source material attached to your agents.' }, { eyebrow: '系统 / 技能', title: '技能目录', description: '附加到 Agent 的已验证指令与来源材料。' }, { eyebrow: 'システム / スキル', title: 'スキルカタログ', description: 'エージェントに添付する検証済みの指示とソース資料です。' }),
  squads: C({ eyebrow: 'ORCHESTRATION / SQUADS', title: 'Agent squads', description: 'Purpose-built agent compositions, coordination rules, and handoffs.' }, { eyebrow: '编排 / AGENT 小组', title: 'Agent 小组', description: '面向特定目标的 Agent 组合、协调规则与交接。' }, { eyebrow: 'オーケストレーション / SQUAD', title: 'エージェント Squad', description: '目的別のエージェント構成、連携ルール、引き継ぎを管理します。' }),
  autopilots: C({ eyebrow: 'ORCHESTRATION / AUTOMATION', title: 'Autopilots', description: 'Scheduled work, next run times, and recent execution outcomes.' }, { eyebrow: '编排 / 自动化', title: '自动任务', description: '查看计划任务、下次运行时间与最近执行结果。' }, { eyebrow: 'オーケストレーション / 自動化', title: '自動タスク', description: 'スケジュール、次回実行、最近の実行結果を確認します。' }),
  usage: C({ eyebrow: 'SYSTEM / OBSERVABILITY', title: 'Usage', description: 'Token, cost, and session trends across the local control plane.' }, { eyebrow: '系统 / 可观测性', title: '用量', description: '本地控制平面的 Token、成本和会话趋势。' }, { eyebrow: 'システム / 可観測性', title: '使用量', description: 'ローカル制御環境のトークン、コスト、セッション傾向です。' }),
  activity: C({ eyebrow: 'SYSTEM / EVIDENCE', title: 'Activity', description: 'A chronological record of actions, targets, and execution outcomes.' }, { eyebrow: '系统 / 证据', title: '活动', description: '按时间记录操作、目标与执行结果。' }, { eyebrow: 'システム / 証跡', title: 'アクティビティ', description: '操作、対象、実行結果を時系列で記録します。' }),
}

export function IssuesRouteView() { const i = useInstanceContext(); return <Collection copy={collections.issues}><IssuesBoard workspaceId={i.workspace_id} /></Collection> }
export function InboxRouteView() { const i = useInstanceContext(); return <Collection copy={collections.inbox}><InboxList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ChatRouteView() { const i = useInstanceContext(); return <Collection copy={collections.chat}><ChatPage client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function AgentsRouteView() { const i = useInstanceContext(); return <Collection copy={collections.agents}><AgentsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ProjectsRouteView() { const i = useInstanceContext(); return <Collection copy={collections.projects}><ProjectsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function RuntimesRouteView() { const i = useInstanceContext(); return <Collection copy={collections.runtimes}><RuntimesList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function SkillsRouteView() { const i = useInstanceContext(); return <Collection copy={collections.skills}><SkillsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function SquadsRouteView() { const i = useInstanceContext(); return <Collection copy={collections.squads}><SquadsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function AutopilotsRouteView() { const i = useInstanceContext(); return <Collection copy={collections.autopilots}><AutopilotsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function UsageRouteView() { const i = useInstanceContext(); return <Collection copy={collections.usage}><UsagePage client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ActivityRouteView() { const i = useInstanceContext(); return <Collection copy={collections.activity}><AuditList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function IssueDetailRouteView({ id }: { id: string }) { const i = useInstanceContext(); return <IssueDetail client={apiClient} workspaceId={i.workspace_id} issueId={id} /> }
export function SessionDetailRouteView({ id }: { id: string }) { return <SessionDetail client={apiClient} sessionId={id} /> }
export function SkillDetailRouteView({ name }: { name: string }) { return <SkillDetail client={apiClient} name={name} /> }
