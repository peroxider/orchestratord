'use client'

import { useState } from 'react'
import { Button } from '@orchestratord/ui'

export interface ChatComposerProps {
  onSend: (content: string) => void
  placeholder?: string
  submitLabel?: string
  busy?: boolean
  disabled?: boolean
}

export function ChatComposer({
  onSend,
  placeholder = 'Message…',
  submitLabel = 'Send',
  busy = false,
  disabled = false,
}: ChatComposerProps) {
  const [value, setValue] = useState('')

  const submit = () => {
    const content = value.trim()
    if (!content || disabled || busy) return
    onSend(content)
    setValue('')
  }

  return (
    <form
      className="chat-composer"
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      <textarea
        className="chat-composer__input"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        rows={3}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            submit()
          }
        }}
      />
      <Button type="submit" size="sm" disabled={disabled || busy || !value.trim()}>
        {submitLabel}
      </Button>
    </form>
  )
}
