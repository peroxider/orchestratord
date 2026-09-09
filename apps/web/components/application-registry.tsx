'use client'

import { createContext, useContext, type ReactNode } from 'react'
import type { ApplicationRegistry } from '@orchestratord/app-contracts'

const RegistryContext = createContext<ApplicationRegistry | null>(null)

export function ApplicationRegistryProvider({ registry, children }: { registry: ApplicationRegistry; children: ReactNode }) {
  return <RegistryContext.Provider value={registry}>{children}</RegistryContext.Provider>
}

export function useApplicationRegistry() {
  const registry = useContext(RegistryContext)
  if (!registry) throw new Error('useApplicationRegistry must be used inside ApplicationRegistryProvider')
  return registry
}
