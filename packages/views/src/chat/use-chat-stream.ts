'use client'

import { useEffect, useMemo, useState } from 'react'
import {
  observeActiveRealtimeClient,
  useRealtimeSubscription,
  type RealtimeMessage,
} from '@orchestratord/core'

interface ChatStreamFrame {
  event?: string
  session_id?: string
  text?: string
  message?: string
}

export interface ChatStream {
  /** Assistant text streamed since the last terminal frame. */
  streamText: string
  /** Message of the last `error` frame, if any. */
  streamError: string | null
}

/**
 * Live tail of one chat session (§6.1): appends `text` / `text_delta` frames
 * from the `chat.{session_id}` realtime topic into a transient assistant
 * bubble. Terminal frames (`turn_complete` / `session_complete` / `error`)
 * clear the buffer — the `chat-messages` invalidation mapping refetches the
 * persisted turn at the same moment, so the folded message replaces the
 * stream without a gap. Degrades to polling when no realtime socket is open.
 */
export function useChatStream(sessionId: string | null): ChatStream {
  const [streamText, setStreamText] = useState('')
  const [streamError, setStreamError] = useState<string | null>(null)

  const topics = useMemo(
    () => (sessionId ? [`chat.${sessionId}`] : []),
    [sessionId],
  )
  useRealtimeSubscription(topics)

  useEffect(() => {
    setStreamText('')
    setStreamError(null)
  }, [sessionId])

  useEffect(() => {
    if (!sessionId) return
    const topic = `chat.${sessionId}`
    let detach: (() => void) | undefined
    const stopObserving = observeActiveRealtimeClient((client) => {
      detach?.()
      detach = client?.addMessageListener((message: RealtimeMessage) => {
        if (message.type !== 'event' || message.topic !== topic) return
        const frame = message.payload as ChatStreamFrame | undefined
        if (!frame) return
        if (frame.event === 'text' || frame.event === 'text_delta') {
          setStreamText((prev) => prev + (frame.text ?? ''))
        } else if (frame.event === 'error') {
          setStreamError(frame.message ?? 'stream error')
          setStreamText('')
        } else if (
          frame.event === 'turn_complete' ||
          frame.event === 'session_complete'
        ) {
          setStreamText('')
          setStreamError(null)
        }
      })
    })
    return () => {
      stopObserving()
      detach?.()
    }
  }, [sessionId])

  return { streamText, streamError }
}
