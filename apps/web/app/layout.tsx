import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import '@orchestratord/ui/tokens.css'
import './globals.css'
import './shell.css'
import './shell.css'
import { WebProviders } from './web-providers'
import { AppShell } from '@/components/app-shell'

export const metadata: Metadata = {
  title: 'orchestratord · Execution Ledger',
  description: 'Local-first agent orchestration control plane',
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <body>
        <WebProviders><AppShell>{children}</AppShell></WebProviders>
      </body>
    </html>
  )
}
