import { describe, expect, it } from 'vitest'
import { useWorkspaceStore } from './workspace'

describe('useWorkspaceStore', () => {
  it('starts empty', () => {
    expect(useWorkspaceStore.getState().workspaceSlug).toBeNull()
    expect(useWorkspaceStore.getState().workspaceId).toBeNull()
  })

  it('setCurrentWorkspace updates both fields', () => {
    useWorkspaceStore.getState().setCurrentWorkspace('acme', 'ws_123')
    expect(useWorkspaceStore.getState().workspaceSlug).toBe('acme')
    expect(useWorkspaceStore.getState().workspaceId).toBe('ws_123')
  })
})
