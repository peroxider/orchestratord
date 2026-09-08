'use client'

import { useEffect } from 'react'
import { observeActiveRealtimeClient } from './useRealtimeBridge'

/**
 * Subscribe the app-wide realtime socket (opened by `useRealtimeBridge`) to
 * *topics* for the lifetime of the calling component; unsubscribes on
 * unmount or whenever the topic list changes (pass a memoized array). A
 * no-op when no socket is active — features degrade to their polling
 * fallbacks.
 */
export function useRealtimeSubscription(topics: string[]): void {
  useEffect(() => {
    if (topics.length === 0) return
    let subscribedClient: Parameters<Parameters<typeof observeActiveRealtimeClient>[0]>[0] = null
    const stopObserving = observeActiveRealtimeClient((client) => {
      if (subscribedClient && subscribedClient !== client) {
        subscribedClient.unsubscribe(topics)
      }
      subscribedClient = client
      client?.subscribe(topics)
    })
    return () => {
      stopObserving()
      subscribedClient?.unsubscribe(topics)
    }
  }, [topics])
}
