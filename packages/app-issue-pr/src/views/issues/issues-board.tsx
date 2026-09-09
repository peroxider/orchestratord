'use client'

import { useState } from 'react'
import type { ApiClient } from '@orchestratord/core'
import { useLocale } from '@orchestratord/views'
import { Button, Input } from '@orchestratord/ui'
import { useCreateIssue } from '../../queries/issues'
import { IssuesList } from './issues-list'
import { KanbanBoard } from './kanban-board'

export function IssuesBoard({ client, workspaceId }: { client: ApiClient; workspaceId: string }) {
  const locale = useLocale()
  const c = {
    en: { list: 'List', board: 'Kanban', placeholder: 'New issue title', create: 'Create' },
    'zh-CN': { list: '列表', board: '看板', placeholder: '新任务标题', create: '创建' },
    ja: { list: 'リスト', board: 'カンバン', placeholder: '新しい Issue のタイトル', create: '作成' },
  }[locale]
  const [view, setView] = useState<'list' | 'kanban'>('list')
  const [title, setTitle] = useState('')
  const create = useCreateIssue(client, workspaceId)

  return (
    <div className="issues-board">
      <div className="issues-board__toolbar">
        <div className="issues-board__toggle">
          <Button
            variant={view === 'list' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setView('list')}
          >
            {c.list}
          </Button>
          <Button
            variant={view === 'kanban' ? 'primary' : 'ghost'}
            size="sm"
            onClick={() => setView('kanban')}
          >
            {c.board}
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
            placeholder={c.placeholder}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <Button type="submit" size="sm" disabled={!title.trim() || create.isPending}>
            {c.create}
          </Button>
        </form>
      </div>

      {view === 'list' ? (
        <IssuesList client={client} workspaceId={workspaceId} />
      ) : (
        <KanbanBoard client={client} workspaceId={workspaceId} />
      )}
    </div>
  )
}
