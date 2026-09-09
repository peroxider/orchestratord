import type { ComponentType, ReactNode } from 'react'

export type Locale = 'en' | 'zh-CN' | 'ja'
export type LocalizedText = Record<Locale, string>

export interface ResourceRef {
  application_id: string | null
  kind: string
  id: string
  label?: string
}

export interface ResourceRelation {
  relation: 'source' | 'produced' | 'uses' | 'belongs_to' | string
  target: ResourceRef
}

export interface ApplicationAvailability {
  id: string
  enabled: boolean
  capabilities: string[]
}

export interface NavContribution {
  id: string
  applicationId: string
  label: LocalizedText
  href: string
  icon: string
  order: number
  capability?: string
}

export interface ActionContext {
  workspaceId: string
  navigate(href: string): void
  close(): void
}

export interface ActionContribution {
  id: string
  applicationId: string
  label: LocalizedText
  icon: string
  order: number
  capability?: string
  shortcut?: string
  render(context: ActionContext): ReactNode
}

export interface SearchContext {
  workspaceId: string
  signal: AbortSignal
}

export interface SearchResult {
  id: string
  title: string
  subtitle?: string
  resource: ResourceRef
  keywords?: string[]
}

export interface SearchProvider {
  id: string
  applicationId: string | null
  kinds: string[]
  search(query: string, context: SearchContext): Promise<SearchResult[]>
}

export interface ResourcePresentation {
  label: string
  href: string | null
  available: boolean
  applicationName?: string
}

export interface ResourcePresenter {
  id: string
  applicationId: string | null
  kind: string
  present(ref: ResourceRef, locale: Locale): ResourcePresentation
}

export interface OverviewWidgetProps {
  workspaceId: string
  locale: Locale
  navigate(href: string): void
}

export interface OverviewWidget {
  id: string
  applicationId: string
  order: number
  span: 1 | 2
  component: ComponentType<OverviewWidgetProps>
}

export interface RealtimeContribution {
  id: string
  applicationId: string
  match(topic: string): boolean
  invalidations(topic: string, workspaceId: string): readonly (readonly unknown[])[]
}

export interface TargetDescriptor {
  id: string
  applicationId: string | null
  kind: string
  label: LocalizedText
  order: number
  capability?: string
}

export interface OnboardingContribution {
  id: string
  applicationId: string
  order: number
  title: LocalizedText
  description: LocalizedText
  actionLabel: LocalizedText
  href: string
}

export interface FrontendApplication {
  id: string
  displayName: LocalizedText
  icon: string
  order: number
  navigation: NavContribution[]
  globalActions: ActionContribution[]
  searchProviders: SearchProvider[]
  resourcePresenters: ResourcePresenter[]
  overviewWidgets?: OverviewWidget[]
  realtime?: RealtimeContribution[]
  autopilotTargets?: TargetDescriptor[]
  onboarding?: OnboardingContribution[]
}

function hasCapability(capabilities: ReadonlySet<string>, capability?: string) {
  return !capability || capabilities.has(capability)
}

export interface ApplicationRegistry {
  applications(): readonly FrontendApplication[]
  navigation(): readonly NavContribution[]
  globalActions(): readonly ActionContribution[]
  searchProviders(): readonly SearchProvider[]
  overviewWidgets(): readonly OverviewWidget[]
  onboarding(): readonly OnboardingContribution[]
  realtime(): readonly RealtimeContribution[]
  autopilotTargets(): readonly TargetDescriptor[]
  resolveResource(ref: ResourceRef, locale: Locale): ResourcePresentation
}

export function createApplicationRegistry(
  applications: readonly FrontendApplication[],
  availability?: readonly ApplicationAvailability[],
): ApplicationRegistry {
  const ids = new Set<string>()
  const contributionIds = new Set<string>()
  const routes = new Set<string>()
  const presenterKeys = new Set<string>()
  for (const application of applications) {
    if (ids.has(application.id)) throw new Error(`Duplicate application id: ${application.id}`)
    ids.add(application.id)
    for (const contribution of [...application.navigation, ...application.globalActions, ...application.searchProviders, ...(application.overviewWidgets ?? []), ...(application.onboarding ?? []), ...(application.realtime ?? []), ...(application.autopilotTargets ?? [])]) {
      if (contributionIds.has(contribution.id)) throw new Error(`Duplicate contribution id: ${contribution.id}`)
      contributionIds.add(contribution.id)
    }
    for (const nav of application.navigation) {
      if (routes.has(nav.href)) throw new Error(`Duplicate application route: ${nav.href}`)
      routes.add(nav.href)
    }
    for (const presenter of application.resourcePresenters) {
      const key = `${presenter.applicationId ?? 'platform'}:${presenter.kind}`
      if (presenterKeys.has(key)) throw new Error(`Duplicate resource presenter: ${key}`)
      presenterKeys.add(key)
    }
  }

  const availabilityMap = new Map(availability?.map(item => [item.id, item]))
  const enabled = [...applications]
    .filter(application => availability === undefined || availabilityMap.get(application.id)?.enabled === true)
    .sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
  const capabilities = new Map(enabled.map(application => [application.id, new Set(availabilityMap.get(application.id)?.capabilities ?? [])]))
  const forEnabled = <T extends { applicationId: string | null; order?: number; capability?: string }>(select: (application: FrontendApplication) => readonly T[]) =>
    enabled.flatMap(select).filter(item => hasCapability(capabilities.get(item.applicationId ?? '') ?? new Set(), item.capability)).sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || ('id' in a && 'id' in b ? String(a.id).localeCompare(String(b.id)) : 0))

  const presenters = new Map(enabled.flatMap(application => application.resourcePresenters).map(presenter => [`${presenter.applicationId ?? 'platform'}:${presenter.kind}`, presenter]))
  const navigation = forEnabled(application => application.navigation)
  const globalActions = forEnabled(application => application.globalActions)
  const searchProviders = enabled.flatMap(application => application.searchProviders)
  const overviewWidgets = forEnabled(application => application.overviewWidgets ?? [])
  const onboarding = forEnabled(application => application.onboarding ?? [])
  const realtime = forEnabled(application => application.realtime ?? [])
  const autopilotTargets = forEnabled(application => application.autopilotTargets ?? [])
  return {
    applications: () => enabled,
    navigation: () => navigation,
    globalActions: () => globalActions,
    searchProviders: () => searchProviders,
    overviewWidgets: () => overviewWidgets,
    onboarding: () => onboarding,
    realtime: () => realtime,
    autopilotTargets: () => autopilotTargets,
    resolveResource: (ref, locale) => {
      const presenter = presenters.get(`${ref.application_id ?? 'platform'}:${ref.kind}`)
      if (presenter) return presenter.present(ref, locale)
      const application = ref.application_id ? enabled.find(item => item.id === ref.application_id) : undefined
      return {
        label: ref.label || `${ref.kind} · ${ref.id}`,
        href: null,
        available: false,
        applicationName: application?.displayName[locale] ?? ref.application_id ?? undefined,
      }
    },
  }
}

export function localized(text: LocalizedText, locale: Locale) { return text[locale] }
