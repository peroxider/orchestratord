'use client'

import { useState } from 'react'
import {
  useAutopilots,
  useCreateAutopilot,
  usePatchAutopilot,
  type ApiClient,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'
import { cronSummary } from './schedule'
import { useLocale } from '../i18n'

export interface AutopilotsListProps {
  client: ApiClient
  workspaceId: string
}

export function AutopilotsList({ client, workspaceId }: AutopilotsListProps) {
  const [name, setName] = useState('')
  const [cron, setCron] = useState('')
  const [prompt, setPrompt] = useState('')
  const [targetKind, setTargetKind] = useState('issue')
  const [targetId, setTargetId] = useState('')

  const { data, isPending, isError, error } = useAutopilots(client, workspaceId)
  const create = useCreateAutopilot(client, workspaceId)
  const locale = useLocale()
  const c = autopilotCopy[locale]

  function submitCreate() {
    if (!name.trim() || !cron.trim() || !targetId.trim()) return
    create.mutate(
      {
        name: name.trim(),
        cron: cron.trim(),
        prompt: prompt.trim(),
        target_kind: targetKind,
        target_id: targetId.trim(),
        enabled: true,
      },
      { onSuccess: () => setName('') },
    )
  }

  return (
    <div className="autopilots">
      <Card className="autopilots__create">
        <div className="autopilots__create-form">
          <label className="autopilots__field autopilots__field--name"><span>{c.name}</span><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={c.name} /></label>
          <label className="autopilots__field autopilots__field--cron"><span>{c.schedule}</span><Input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 * * * *" />{cron.trim() && <small className="autopilots__schedule-preview">{cronSummary(cron, locale)}</small>}</label>
          <label className="autopilots__field autopilots__field--prompt"><span>{c.prompt}</span><Input value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={c.prompt} /></label>
          <label className="autopilots__field autopilots__field--kind"><span>{c.targetKind}</span><select value={targetKind} onChange={(e) => setTargetKind(e.target.value)}><option value="issue">Issue</option><option value="squad">Squad</option></select></label>
          <label className="autopilots__field autopilots__field--target"><span>{c.targetId}</span><Input value={targetId} onChange={(e) => setTargetId(e.target.value)} placeholder={c.targetId} /></label>
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending || !name.trim() || !cron.trim() || !targetId.trim()}
            onClick={submitCreate}
          >
            {c.create}
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="autopilots__empty">{c.loading}</p>
      ) : isError ? (
        <p className="autopilots__empty">
          {c.failed}: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="autopilots__empty">{c.empty}</p>
      ) : (
        <div className="autopilots__grid">
          {(data ?? []).map((autopilot) => (
            <AutopilotCard
              key={autopilot.id}
              workspaceId={workspaceId}
              client={client}
              autopilotId={autopilot.id}
              name={autopilot.name}
              cron={autopilot.cron}
              targetKind={autopilot.target_kind}
              enabled={autopilot.enabled}
              locale={locale}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AutopilotCard({
  workspaceId,
  client,
  autopilotId,
  name,
  cron,
  targetKind,
  enabled,
  locale,
}: {
  workspaceId: string
  client: ApiClient
  autopilotId: string
  name: string
  cron: string
  targetKind: string
  enabled: boolean
  locale: keyof typeof autopilotCopy
}) {
  const patch = usePatchAutopilot(client, workspaceId, autopilotId)
  const c = autopilotCopy[locale]
  return (
    <Card className="autopilot-card">
      <header className="autopilot-card__header">
        <a className="autopilot-card__name" href={`/autopilots/${autopilotId}`}>{name}</a>
        <Badge tone={enabled ? 'good' : 'neutral'}>
          {enabled ? c.enabled : c.disabled}
        </Badge>
      </header>
      <p className="autopilot-card__cron"><strong>{cronSummary(cron, locale)}</strong><code>{cron}</code></p>
      <p className="autopilot-card__target">{c.target}: {targetKind}</p>
      <p className="autopilot-card__next">{c.next}</p>
      <div className="autopilot-card__actions">
        <Button
          size="sm"
          variant={enabled ? 'secondary' : 'primary'}
          disabled={patch.isPending}
          onClick={() => patch.mutate({ enabled: !enabled })}
        >
          {enabled ? c.disable : c.enable}
        </Button>
      </div>
    </Card>
  )
}

const autopilotCopy = {
  en: { name: 'Name', schedule: 'Schedule', prompt: 'Prompt', targetKind: 'Target kind', targetId: 'Target id', create: 'Create', loading: 'Loading autopilots…', failed: 'Failed to load autopilots', empty: 'No autopilots yet.', enabled: 'enabled', disabled: 'disabled', target: 'Target', next: 'Next run will appear when the scheduler reports it.', disable: 'Pause', enable: 'Enable' },
  'zh-CN': { name: '名称', schedule: '计划', prompt: '提示词', targetKind: '目标类型', targetId: '目标 ID', create: '创建', loading: '正在加载自动任务…', failed: '无法加载自动任务', empty: '暂无自动任务。', enabled: '已启用', disabled: '已暂停', target: '目标', next: '调度器上报后将在此显示下次运行时间。', disable: '暂停', enable: '启用' },
  ja: { name: '名前', schedule: 'スケジュール', prompt: 'プロンプト', targetKind: '対象タイプ', targetId: '対象 ID', create: '作成', loading: '自動タスクを読み込み中…', failed: '自動タスクを読み込めませんでした', empty: '自動タスクはまだありません。', enabled: '有効', disabled: '一時停止', target: '対象', next: 'スケジューラーから報告されると次回実行を表示します。', disable: '一時停止', enable: '有効化' },
} as const
