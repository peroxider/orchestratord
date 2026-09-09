'use client'

import { useState } from 'react'

import { Button, Textarea } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { translate } from '../i18n/dictionaries'
import { InboxCardShell } from './inbox-shared'
import type { InboxKindCardProps } from './inbox-shared'

/** §7.4 clarification view — answer-first framing (resolve = answered). */
export function ClarificationCard({
  item,
  workspaceId,
  busy,
  onDismiss,
  onAnswer,
  resolveResource,
}: InboxKindCardProps) {
  const { locale } = useTranslation()
  const [answer, setAnswer] = useState('')
  const labels = {
    en: { placeholder: 'Answer the agent’s question…', submit: 'Send answer' },
    'zh-CN': { placeholder: '回答 Agent 的问题…', submit: '发送回答' },
    ja: { placeholder: 'エージェントの質問に回答…', submit: '回答を送信' },
  }[locale]
  return (
    <InboxCardShell
      item={item}
      workspaceId={workspaceId}
      resolveResource={resolveResource}
      actions={
        <div className="inbox-card__clarification"><Textarea value={answer} onChange={event => setAnswer(event.target.value)} placeholder={labels.placeholder} disabled={busy} /><div className="inbox-card__actions">
          <Button size="sm" variant="primary" disabled={busy || !answer.trim() || !onAnswer} onClick={() => onAnswer?.(answer.trim())}>
            {labels.submit}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={onDismiss}>
            {translate(locale, 'inbox.action.dismiss', 'Dismiss')}
          </Button>
        </div></div>
      }
    >
      <p className="inbox-card__hint">
        {translate(
          locale,
          'inbox.hint.clarification',
          'The agent asked a clarifying question.',
        )}
      </p>
    </InboxCardShell>
  )
}
