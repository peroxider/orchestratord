import { describe, expect, it } from 'vitest'
import { createApplicationRegistry, type FrontendApplication } from './index'

const text = { en: 'Example', 'zh-CN': '示例', ja: '例' }
function app(id: string, href = `/${id}`): FrontendApplication {
  return { id, displayName: text, icon: 'issues', order: 10, navigation: [{ id: `${id}.nav`, applicationId: id, label: text, href, icon: 'issues', order: 10 }], globalActions: [], searchProviders: [], resourcePresenters: [] }
}

describe('application registry', () => {
  it('filters disabled applications and keeps a deterministic order', () => {
    const registry = createApplicationRegistry([{ ...app('b'), order: 20 }, app('a')], [{ id: 'a', enabled: true, capabilities: [] }, { id: 'b', enabled: false, capabilities: [] }])
    expect(registry.applications().map(item => item.id)).toEqual(['a'])
    expect(registry.navigation().map(item => item.href)).toEqual(['/a'])
  })

  it('rejects duplicate ids, routes, and contribution ids', () => {
    expect(() => createApplicationRegistry([app('a'), app('a')])).toThrow(/Duplicate application id/)
    expect(() => createApplicationRegistry([app('a', '/same'), app('b', '/same')])).toThrow(/Duplicate application route/)
    const b = app('b'); b.navigation[0]!.id = 'a.nav'
    expect(() => createApplicationRegistry([app('a'), b])).toThrow(/Duplicate contribution id/)
  })

  it('returns an inspectable fallback for an unknown resource', () => {
    const result = createApplicationRegistry([]).resolveResource({ application_id: 'future_app', kind: 'record', id: 'r-1' }, 'en')
    expect(result).toMatchObject({ label: 'record · r-1', href: null, available: false, applicationName: 'future_app' })
  })

  it('adds a mock SOP application through contributions only', async () => {
    const sopText = { en: 'SOP Agent', 'zh-CN': 'SOP Agent', ja: 'SOP Agent' }
    const sop: FrontendApplication = {
      id: 'sop_agent', displayName: sopText, icon: 'agents', order: 20,
      navigation: [{ id: 'sop.nav', applicationId: 'sop_agent', label: sopText, href: '/sop', icon: 'agents', order: 20 }],
      globalActions: [{ id: 'sop.create', applicationId: 'sop_agent', label: sopText, icon: 'plus', order: 20, shortcut: 'C S', render: () => null }],
      searchProviders: [{ id: 'sop.search', applicationId: 'sop_agent', kinds: ['procedure'], search: async () => [{ id: 'p-1', title: 'Release procedure', resource: { application_id: 'sop_agent', kind: 'procedure', id: 'p-1' } }] }],
      resourcePresenters: [{ id: 'sop.resource', applicationId: 'sop_agent', kind: 'procedure', present: ref => ({ label: `SOP ${ref.id}`, href: `/sop/${ref.id}`, available: true }) }],
    }
    const registry = createApplicationRegistry([app('issue_pr', '/issues'), sop], [
      { id: 'issue_pr', enabled: true, capabilities: [] },
      { id: 'sop_agent', enabled: true, capabilities: [] },
    ])

    expect(registry.navigation().map(item => item.href)).toEqual(['/issues', '/sop'])
    expect(registry.globalActions().map(item => item.id)).toContain('sop.create')
    await expect(registry.searchProviders()[0]!.search('', { workspaceId: 'w-1', signal: new AbortController().signal })).resolves.toMatchObject([{ title: 'Release procedure' }])
    expect(registry.resolveResource({ application_id: 'sop_agent', kind: 'procedure', id: 'p-1' }, 'en')).toMatchObject({ label: 'SOP p-1', href: '/sop/p-1', available: true })
  })
})
