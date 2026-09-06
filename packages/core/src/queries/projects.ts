import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ApiClient } from '../api/client'
import type { Project, ProjectDoc, ProjectRepo } from '../api/types'

export function useProjects(client: ApiClient, workspaceId: string) {
  return useQuery({
    queryKey: ['projects', workspaceId],
    queryFn: () =>
      client.request<Project[]>(`/api/workspaces/${workspaceId}/projects`),
  })
}

export function useProject(
  client: ApiClient,
  workspaceId: string,
  projectId: string,
) {
  return useQuery({
    queryKey: ['projects', workspaceId, projectId],
    queryFn: () =>
      client.request<Project>(
        `/api/workspaces/${workspaceId}/projects/${projectId}`,
      ),
  })
}

export interface CreateProjectInput {
  name: string
  description?: string
}

export function useCreateProject(client: ApiClient, workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateProjectInput) =>
      client.request<Project>(`/api/workspaces/${workspaceId}/projects`, {
        method: 'POST',
        body: JSON.stringify(input),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['projects', workspaceId] }),
  })
}

export interface AddProjectRepoInput {
  repo_url: string
  default_branch?: string
}

export function useAddProjectRepo(
  client: ApiClient,
  workspaceId: string,
  projectId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: AddProjectRepoInput) =>
      client.request<ProjectRepo>(
        `/api/workspaces/${workspaceId}/projects/${projectId}/repos`,
        { method: 'POST', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['projects', workspaceId, projectId],
      }),
  })
}

export interface AddProjectDocInput {
  doc_url: string
  doc_type: string
}

export function useAddProjectDoc(
  client: ApiClient,
  workspaceId: string,
  projectId: string,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: AddProjectDocInput) =>
      client.request<ProjectDoc>(
        `/api/workspaces/${workspaceId}/projects/${projectId}/docs`,
        { method: 'POST', body: JSON.stringify(input) },
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ['projects', workspaceId, projectId],
      }),
  })
}
