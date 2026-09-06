import { loader } from 'fumadocs-core/source'
import { defineDocs } from 'fumadocs-mdx/macro'
import { i18n } from '@/lib/i18n'

const docs = defineDocs({ dir: 'content/docs' })

export const source = loader({
  i18n,
  baseUrl: '/docs',
  source: docs.toFumadocsSource(),
})
