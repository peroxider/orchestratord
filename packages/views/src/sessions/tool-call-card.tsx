'use client'

import { useState } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'
import { redactSensitive } from './redact-sensitive'
import { useLocale } from '../i18n'

export interface ToolCallCardProps {
  event: SessionEvent
  /** Override the truncation ceiling. Defaults to 4096 to match
   * ``chat_gateway.py::_MAX_TOOL_RESULT_CHARS``. */
  truncateAt?: number
}

const DEFAULT_TRUNCATE_AT = 4096

export function ToolCallCard({ event, truncateAt = DEFAULT_TRUNCATE_AT }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false)
  const c = evidenceCopy[useLocale()]
  const p = event.payload
  const name = typeof p.name === 'string' ? p.name : c.toolCall
  const callId = typeof p.call_id === 'string' ? p.call_id : null
  const args =
    p.arguments && typeof p.arguments === 'object'
      ? (p.arguments as Record<string, unknown>)
      : {}
  const serialized = JSON.stringify(redactSensitive(args), null, 2)
  const isTruncated = serialized.length > truncateAt
  const preview = isTruncated ? serialized.slice(0, truncateAt) + '…' : serialized
  const showFull = expanded || !isTruncated

  return (
    <Card className="tool-call-card" data-call-id={callId ?? undefined}>
      <header className="tool-call-card__header">
        <Badge tone="purple">tool_call</Badge>
        <span className="tool-call-card__name">{name}</span>
        {callId && (
          <span className="tool-call-card__call-id" title={callId}>
            {callId.length > 12 ? `${callId.slice(0, 12)}…` : callId}
          </span>
        )}
      </header>
      <pre className="tool-call-card__args">
        {showFull ? serialized : preview}
      </pre>
      {isTruncated && (
        <footer className="tool-call-card__footer">
          <span className="tool-call-card__truncated-note">
            {c.truncated} ({serialized.length.toLocaleString()} {c.characters} &gt; {truncateAt.toLocaleString()})
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

const evidenceCopy = {
  en: { toolCall: 'tool call', truncated: 'truncated', characters: 'characters', collapse: 'Collapse', viewFull: 'View full' },
  'zh-CN': { toolCall: '工具调用', truncated: '已截断', characters: '个字符', collapse: '收起', viewFull: '查看完整内容' },
  ja: { toolCall: 'ツール呼び出し', truncated: '省略済み', characters: '文字', collapse: '折りたたむ', viewFull: 'すべて表示' },
} as const
