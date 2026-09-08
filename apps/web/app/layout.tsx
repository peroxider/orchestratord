import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import '@orchestratord/ui/tokens.css'
import './globals.css'
import { WebProviders } from './web-providers'

export const metadata: Metadata = {
  title: 'orchestratord',
  description: 'AI task management platform',
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <body>
        <WebProviders>{children}</WebProviders>
      </body>
    </html>
  )
}
