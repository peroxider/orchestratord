'use client'

import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RealtimeClient } from './client'
import { invalidationFor } from './messages'

export interface UseRealtimeBridgeOptions {
  url: string
  workspaceId: string
  token?: string
}

/**
 * Bridge the realtime WebSocket to TanStack Query (§5.4.3): every
 * server-state frame is turned into a targeted `invalidateQueries` call, so
 * React Query refetches from the REST API instead of the bridge hand-merging
 * cache entries.
 */
export function useRealtimeBridge({
  url,
  workspaceId,
  token,
}: UseRealtimeBridgeOptions): void {
  const queryClient = useQueryClient()

  useEffect(() => {
    const client = new RealtimeClient({
      url,
      workspaceId,
      token,
      onMessage: (message) => {
        const keys = invalidationFor(message, workspaceId)
        if (keys) {
          for (const key of keys) {
            void queryClient.invalidateQueries({ queryKey: key })
          }
        }
      },
    })
    client.connect()
    return () => client.disconnect()
  }, [url, workspaceId, token, queryClient])
}
