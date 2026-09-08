import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared'
import { uiTranslations } from 'fumadocs-ui/i18n'
import { i18n } from '@/lib/i18n'

export const translations = i18n
  .translations()
  .extend(uiTranslations())
  .add({
    en: { displayName: 'English' },
    'zh-CN': { displayName: '简体中文' },
  } as Record<'en' | 'zh-CN', Record<string, string>>)

export function baseOptions(locale: string): BaseLayoutProps {
  return {
    nav: {
      title: 'orchestratord',
      url: `/${locale}`,
    },
    links: [
      {
        type: 'main',
        text: locale === 'zh-CN' ? '文档' : 'Documentation',
        url: `/${locale}/docs`,
        active: 'nested-url',
      },
    ],
  }
}
