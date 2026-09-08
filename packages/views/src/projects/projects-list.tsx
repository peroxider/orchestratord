'use client'

import { useState } from 'react'
import {
  useCreateProject,
  useProjects,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'
import { useLocale } from '../i18n'

export interface ProjectsListProps {
  client: ApiClient
  workspaceId: string
}

export function ProjectsList({ client, workspaceId }: ProjectsListProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  const { data, isPending, isError, error } = useProjects(client, workspaceId)
  const create = useCreateProject(client, workspaceId)
  const locale = useLocale()
  const c = {
    en: { name: 'Project name', description: 'Description', create: 'Create', loading: 'Loading projects…', failed: 'Failed to load projects', empty: 'No projects yet.', repos: 'repos', docs: 'docs', active: 'Active' },
    'zh-CN': { name: '项目名称', description: '说明', create: '创建', loading: '正在加载项目…', failed: '无法加载项目', empty: '暂无项目。', repos: '个仓库', docs: '份文档', active: '进行中' },
    ja: { name: 'プロジェクト名', description: '説明', create: '作成', loading: 'プロジェクトを読み込み中…', failed: 'プロジェクトを読み込めませんでした', empty: 'プロジェクトはまだありません。', repos: 'リポジトリ', docs: 'ドキュメント', active: '進行中' },
  }[locale]

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
            placeholder={c.name}
            aria-label={c.name}
          />
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={c.description}
            aria-label={c.description}
          />
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim()}
            onClick={submitCreate}
          >
            {c.create}
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="projects__empty">{c.loading}</p>
      ) : isError ? (
        <p className="projects__empty">
          {c.failed}: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="projects__empty">{c.empty}</p>
      ) : (
        <div className="projects__grid">
          {(data ?? []).map((project) => (
            <Card key={project.id} className="project-card">
              <header className="project-card__header">
                <span className="project-card__name">{project.name}</span>
                <div><Badge tone="good">{c.active}</Badge><Badge tone="neutral">{project.repos.length} {c.repos}</Badge></div>
              </header>
              {project.description && (
                <p className="project-card__description">{project.description}</p>
              )}
              <p className="project-card__meta">{project.docs.length} {c.docs}</p>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
