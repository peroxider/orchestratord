'use client'
import { IssuesApplicationRoute } from '@/lib/registered-applications'
import { useInstanceContext } from '@/components/app-shell'
import { useLocale } from '@orchestratord/views'

const copy = { en: ['ISSUE → PR / WORK ITEMS', 'Issues', 'Create, sort, and advance work from intent to verified result.'], 'zh-CN': ['任务 → PR / 工作项', '任务', '创建、整理任务，并从目标推进到可验证的结果。'], ja: ['ISSUE → PR / 作業項目', 'Issue', '作業を作成・整理し、意図から検証済みの結果まで進めます。'] } as const
export default function Page() { const i = useInstanceContext(); const c = copy[useLocale()]; return <div className="collection-page"><header className="collection-intro"><div><p className="section-eyebrow">{c[0]}</p><h2>{c[1]}</h2></div><p>{c[2]}</p></header><IssuesApplicationRoute workspaceId={i.workspace_id} /></div> }
