'use client'

import { useState } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { redactSensitive } from './redact-sensitive'
import { useLocale } from '../i18n'

export interface ToolResultCardProps {
  event: SessionEvent
  /** Override the truncation ceiling. Defaults to 4096 to match
   * ``chat_gateway.py::_MAX_TOOL_RESULT_CHARS``. */
  truncateAt?: number
}

const DEFAULT_TRUNCATE_AT = 4096

export function ToolResultCard({
  event,
  truncateAt = DEFAULT_TRUNCATE_AT,
}: ToolResultCardProps) {
  const [expanded, setExpanded] = useState(false)
  const c = resultCopy[useLocale()]
  const p = event.payload
  const name = typeof p.name === 'string' ? p.name : c.toolResult
  const outputRaw = p.output
  const isError = p.is_error === true
  const backendTruncated = p.truncated === true

  // Normalize the output to a string we can measure + preview.
  let serialized: string
  if (typeof outputRaw === 'string') {
    serialized = String(redactSensitive(outputRaw))
  } else if (outputRaw !== undefined && outputRaw !== null) {
    try {
      serialized = JSON.stringify(redactSensitive(outputRaw), null, 2)
    } catch {
      serialized = String(outputRaw)
    }
  } else {
    serialized = ''
  }

  // Front-end truncation kicks in only when the backend didn't already
  // truncate (the spec says respect backend truncation; if it's
  // missing we still cap the preview so 100KB stdout blobs don't
  // crash the timeline).
  const isTruncated = backendTruncated || serialized.length > truncateAt
  const preview = isTruncated ? serialized.slice(0, truncateAt) + '…' : serialized
  const showFull = expanded || !isTruncated

  return (
    <Card
      className={`tool-result-card${isError ? ' tool-result-card--error' : ''}`}
    >
      <header className="tool-result-card__header">
        <Badge tone={isError ? 'bad' : 'neutral'}>
          {isError ? 'tool_result (error)' : 'tool_result'}
        </Badge>
        <span className="tool-result-card__name">{name}</span>
        {backendTruncated && (
          <Badge tone="warn">{c.backendTruncated}</Badge>
        )}
      </header>
      <pre className="tool-result-card__output">
        {showFull ? serialized : preview}
      </pre>
      {isTruncated && (
        <footer className="tool-result-card__footer">
          <span className="tool-result-card__truncated-note">
            {serialized.length.toLocaleString()} {c.characters}
            {!backendTruncated &&
              ` > ${truncateAt.toLocaleString()} (${c.uiTruncated})`}
          </span>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? c.collapse : c.viewFull}
          </Button>
        </footer>
      )}
    </Card>
  )
}

const resultCopy = {
  en: { toolResult: 'tool result', backendTruncated: 'truncated by backend', characters: 'characters', uiTruncated: 'truncated in UI', collapse: 'Collapse', viewFull: 'View full' },
  'zh-CN': { toolResult: '工具结果', backendTruncated: '后端已截断', characters: '个字符', uiTruncated: '界面已截断', collapse: '收起', viewFull: '查看完整内容' },
  ja: { toolResult: 'ツール結果', backendTruncated: 'バックエンドで省略', characters: '文字', uiTruncated: 'UI で省略', collapse: '折りたたむ', viewFull: 'すべて表示' },
} as const
