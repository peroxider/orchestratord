'use client'

import { useState } from 'react'
import { useCreateIssue, useWorkspaceStore } from '@orchestratord/core'
import { IssuesList, KanbanBoard } from '@orchestratord/views'
import { Button, Input } from '@orchestratord/ui'
import { apiClient } from '@/lib/api'

export function IssuesBoard({ slug }: { slug: string }) {
  const workspaceId = useWorkspaceStore((s) => s.workspaceId) ?? slug
  const [view, setView] = useState<'list' | 'kanban'>('list')
  const [title, setTitle] = useState('')
  const create = useCreateIssue(apiClient, workspaceId)

  return (
    <div className="issues-board">
      <div className="issues-board__toolbar">
        <div className="issues-board__toggle">
          <Button
            variant={view === 'list' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setView('list')}
          >
            List
          </Button>
          <Button
            variant={view === 'kanban' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setView('kanban')}
          >
            Kanban
          </Button>
        </div>
        <form
          className="issues-board__create"
          onSubmit={(e) => {
            e.preventDefault()
            if (!title.trim()) return
            create.mutate({ title })
            setTitle('')
          }}
        >
          <Input
            placeholder="New issue title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <Button type="submit" size="sm" disabled={!title.trim() || create.isPending}>
            Create
          </Button>
        </form>
      </div>

      {view === 'list' ? (
        <IssuesList client={apiClient} workspaceId={workspaceId} />
      ) : (
        <KanbanBoard client={apiClient} workspaceId={workspaceId} />
      )}
    </div>
  )
}
