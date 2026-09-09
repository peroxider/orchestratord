import { createApplicationRegistry, type ApplicationAvailability, type ApplicationRegistry, type Locale, type ResourceRef, type ResourcePresentation } from '@orchestratord/app-contracts'
import { createIssuePrApplication, IssueDetail, IssuesBoard } from '@orchestratord/app-issue-pr'
import type { InstanceBootstrap } from '@orchestratord/core'
import { apiClient } from './api'

const bundledApplications = [createIssuePrApplication(apiClient)]

function configuredApplications(instance: InstanceBootstrap): ApplicationAvailability[] | undefined {
  if (Array.isArray(instance.applications)) return instance.applications
  const fromFeatures = instance.features.applications
  if (Array.isArray(fromFeatures)) return fromFeatures.filter((item): item is ApplicationAvailability => Boolean(item && typeof item === 'object' && 'id' in item && 'enabled' in item && 'capabilities' in item))
  // Compatibility while older servers do not yet advertise applications.
  return [{ id: 'issue_pr', enabled: true, capabilities: ['issues.read', 'issues.write', 'pull_requests.read', 'clarification.respond'] }]
}

export function registryForInstance(instance: InstanceBootstrap) {
  return createApplicationRegistry(bundledApplications, configuredApplications(instance))
}

const platformRoutes: Record<string, string> = { session: 'sessions', agent: 'agents', runtime: 'runtimes', project: 'projects', squad: 'squads', autopilot: 'autopilots', skill: 'skills' }
export function resolveResource(registry: ApplicationRegistry, ref: ResourceRef, locale: Locale): ResourcePresentation {
  if (ref.application_id === null && platformRoutes[ref.kind]) return { label: ref.label || `${ref.kind} · ${ref.id}`, href: `/${platformRoutes[ref.kind]}/${encodeURIComponent(ref.id)}`, available: true }
  return registry.resolveResource(ref, locale)
}

export function IssuesApplicationRoute({ workspaceId }: { workspaceId: string }) {
  return <IssuesBoard client={apiClient} workspaceId={workspaceId} />
}

export function IssueApplicationDetailRoute({ workspaceId, id }: { workspaceId: string; id: string }) {
  return <IssueDetail client={apiClient} workspaceId={workspaceId} issueId={id} />
}
