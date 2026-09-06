import { create } from 'zustand'

export interface WorkspaceState {
  workspaceSlug: string | null
  workspaceId: string | null
  setCurrentWorkspace: (slug: string, id: string) => void
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  workspaceSlug: null,
  workspaceId: null,
  setCurrentWorkspace: (slug, id) =>
    set({ workspaceSlug: slug, workspaceId: id }),
}))
