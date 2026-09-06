'use client'

import { useState } from 'react'
import {
  useCreateProject,
  useProjects,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'

export interface ProjectsListProps {
  client: ApiClient
  workspaceId: string
}

export function ProjectsList({ client, workspaceId }: ProjectsListProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const { data, isPending, isError, error } = useProjects(client, workspaceId)
  const create = useCreateProject(client, workspaceId)

  function submitCreate() {
    if (!name.trim()) return
    create.mutate(
      { name: name.trim(), description: description.trim() },
      { onSuccess: () => setName('') },
    )
  }

  return (
    <div className="projects">
      <Card className="projects__create">
        <div className="projects__create-form">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Project name"
            aria-label="Project name"
          />
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Description"
            aria-label="Description"
          />
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim()}
            onClick={submitCreate}
          >
            Create
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="projects__empty">Loading projects…</p>
      ) : isError ? (
        <p className="projects__empty">
          Failed to load projects: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="projects__empty">No projects yet.</p>
      ) : (
        <div className="projects__grid">
          {(data ?? []).map((project) => (
            <Card key={project.id} className="project-card">
              <header className="project-card__header">
                <span className="project-card__name">{project.name}</span>
                <Badge tone="neutral">{project.repos.length} repo(s)</Badge>
              </header>
              {project.description && (
                <p className="project-card__description">{project.description}</p>
              )}
              <p className="project-card__meta">{project.docs.length} doc(s)</p>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
