import { source } from '@/lib/source'
import { DocsLayout } from 'fumadocs-ui/layouts/docs'
import { baseOptions } from '@/lib/layout.shared'
import type { ReactNode } from 'react'

export default async function DocsLayoutPage({
  params,
  children,
}: {
  params: Promise<{ lang: string }>
  children: ReactNode
}) {
  const { lang } = await params
  const tree = source.getPageTree(lang)
  return (
    <DocsLayout tree={tree} {...baseOptions(lang)}>
      {children}
    </DocsLayout>
  )
}
