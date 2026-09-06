import 'fumadocs-ui/style.css'
import { RootProvider } from 'fumadocs-ui/provider/next'
import { translations } from '@/lib/layout.shared'
import { i18nProvider } from 'fumadocs-ui/i18n'
import type { ReactNode } from 'react'

export default async function Layout({
  params,
  children,
}: {
  params: Promise<{ lang: string }>
  children: ReactNode
}) {
  const { lang } = await params
  return (
    <html lang={lang} suppressHydrationWarning>
      <body>
        <RootProvider i18n={i18nProvider(translations, lang)}>{children}</RootProvider>
      </body>
    </html>
  )
}
