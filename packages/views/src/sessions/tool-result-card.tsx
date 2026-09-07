'use client'

import { useState } from 'react'
import type { SessionEvent } from '@orchestratord/core'
import { Badge, Button, Card } from '@orchestratord/ui'

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
  const p = event.payload
  const name = typeof p.name === 'string' ? p.name : 'tool result'
  const outputRaw = p.output
  const isError = p.is_error === true
  const backendTruncated = p.truncated === true

  // Normalize the output to a string we can measure + preview.
  let serialized: string
  if (typeof outputRaw === 'string') {
    serialized = outputRaw
  } else if (outputRaw !== undefined && outputRaw !== null) {
    try {
      serialized = JSON.stringify(outputRaw, null, 2)
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
          <Badge tone="warn">truncated by backend</Badge>
        )}
      </header>
      <pre className="tool-result-card__output">
        {showFull ? serialized : preview}
      </pre>
      {isTruncated && (
        <footer className="tool-result-card__footer">
          <span className="tool-result-card__truncated-note">
            {serialized.length.toLocaleString()} chars
            {!backendTruncated &&
              ` > ${truncateAt.toLocaleString()} (truncated in UI)`}
          </span>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? 'collapse' : 'view full'}
          </Button>
        </footer>
      )}
    </Card>
  )
}