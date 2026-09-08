import type { Locale } from './types'
import { en } from './locales/en'
import type { TranslationKey } from './locales/en'
import { zhCN } from './locales/zh-CN'
import { ja } from './locales/ja'

export type { Locale } from './types'
export type { TranslationKey } from './locales/en'

export const DICTIONARIES: Record<Locale, Record<TranslationKey, string>> = {
  en,
  'zh-CN': zhCN,
  ja,
}

/**
 * Resolve a key in the given locale, falling back to English, then to `fallback`.
 * The `fallback` default is the raw key so an untranslated/unknown key degrades
 * to a non-empty string rather than `undefined`.
 */
export function translate(
  locale: Locale,
  key: TranslationKey,
  fallback: string = key,
): string {
  return DICTIONARIES[locale][key] ?? en[key] ?? fallback
}
