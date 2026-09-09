'use client'

import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RealtimeClient } from './client'
import type { RealtimeStatus } from './client'
import { invalidationFor } from './messages'
import type { RealtimeContribution } from '@orchestratord/app-contracts'

export interface UseRealtimeBridgeOptions {
  url: string
  workspaceId: string
  token?: string
  onStatus?: (status: RealtimeStatus) => void
  applicationContributions?: readonly RealtimeContribution[]
}

// The dashboard shell opens ONE authenticated socket for the whole app;
// feature hooks (e.g. the chat stream) reuse it instead of opening their own.
let activeClient: RealtimeClient | null = null
const activeClientListeners = new Set<(client: RealtimeClient | null) => void>()

export function getActiveRealtimeClient(): RealtimeClient | null {
  return activeClient
}

export function observeActiveRealtimeClient(
  listener: (client: RealtimeClient | null) => void,
): () => void {
  activeClientListeners.add(listener)
  listener(activeClient)
  return () => activeClientListeners.delete(listener)
}

function setActiveRealtimeClient(client: RealtimeClient | null): void {
  activeClient = client
  for (const listener of activeClientListeners) listener(client)
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
  onStatus,
  applicationContributions = [],
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
        const topic = typeof message.topic === 'string' ? message.topic : ''
        for (const contribution of applicationContributions) {
          if (!contribution.match(topic)) continue
          for (const key of contribution.invalidations(topic, workspaceId)) void queryClient.invalidateQueries({ queryKey: key })
        }
      },
      onStatus,
    })
    setActiveRealtimeClient(client)
    client.connect()
    return () => {
      client.disconnect()
      if (activeClient === client) {
        setActiveRealtimeClient(null)
      }
    }
  }, [url, workspaceId, token, onStatus, queryClient, applicationContributions])
}
