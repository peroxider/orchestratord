'use client'

import type { ReactNode } from 'react'

import {
  AgentsList, AuditList, AutopilotsList, ChatPage, InboxList, IssueDetail,
  ProjectsList, RuntimesList, SessionDetail, SkillDetail, SkillsList,
  SquadsList, UsagePage,
} from '@orchestratord/views'
import { apiClient } from '@/lib/api'
import { useInstanceContext } from './app-shell'
import { IssuesBoard } from './issues-board'

function Collection({ eyebrow, title, description, children }: { eyebrow: string; title: string; description: string; children: ReactNode }) {
  return <div className="collection-page"><header className="collection-intro"><div><p className="section-eyebrow">{eyebrow}</p><h2>{title}</h2></div><p>{description}</p></header>{children}</div>
}

export function IssuesRouteView() { const i = useInstanceContext(); return <Collection eyebrow="WORK / LEDGER" title="Issues" description="Create, sort, and advance work from intent to verified result."><IssuesBoard slug={i.workspace_id} /></Collection> }
export function InboxRouteView() { const i = useInstanceContext(); return <Collection eyebrow="ATTENTION / OPEN" title="Needs your decision" description="Approvals, questions, and failed work are collected here until resolved."><InboxList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ChatRouteView() { const i = useInstanceContext(); return <Collection eyebrow="ATTENTION / CONVERSATIONS" title="Agent conversations" description="Continue a task, inspect live responses, or open its execution record."><ChatPage client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function AgentsRouteView() { const i = useInstanceContext(); return <Collection eyebrow="ORCHESTRATION / AGENTS" title="Execution agents" description="Backends, runtime bindings, and the capabilities each agent can exercise."><AgentsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ProjectsRouteView() { const i = useInstanceContext(); return <Collection eyebrow="WORK / PROJECTS" title="Projects" description="Group related issues and execution context without permission boundaries."><ProjectsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function RuntimesRouteView() { const i = useInstanceContext(); return <Collection eyebrow="SYSTEM / RUNTIMES" title="Runtime nodes" description="Machines and backends available to execute agent work."><RuntimesList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function SkillsRouteView() { const i = useInstanceContext(); return <Collection eyebrow="SYSTEM / SKILLS" title="Skill catalog" description="Verified instructions and source material attached to your agents."><SkillsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function SquadsRouteView() { const i = useInstanceContext(); return <Collection eyebrow="ORCHESTRATION / SQUADS" title="Agent squads" description="Purpose-built agent compositions, coordination rules, and handoffs."><SquadsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function AutopilotsRouteView() { const i = useInstanceContext(); return <Collection eyebrow="ORCHESTRATION / AUTOMATION" title="Autopilots" description="Scheduled work, next run times, and recent execution outcomes."><AutopilotsList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function UsageRouteView() { const i = useInstanceContext(); return <Collection eyebrow="SYSTEM / OBSERVABILITY" title="Usage" description="Token, cost, and session trends across the local control plane."><UsagePage client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function ActivityRouteView() { const i = useInstanceContext(); return <Collection eyebrow="SYSTEM / EVIDENCE" title="Activity" description="A chronological record of actions, targets, and execution outcomes."><AuditList client={apiClient} workspaceId={i.workspace_id} /></Collection> }
export function IssueDetailRouteView({ id }: { id: string }) { const i = useInstanceContext(); return <IssueDetail client={apiClient} workspaceId={i.workspace_id} issueId={id} /> }
export function SessionDetailRouteView({ id }: { id: string }) { return <SessionDetail client={apiClient} sessionId={id} /> }
export function SkillDetailRouteView({ name }: { name: string }) { return <SkillDetail client={apiClient} name={name} /> }
