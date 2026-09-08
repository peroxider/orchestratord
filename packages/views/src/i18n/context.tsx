'use client'

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { Locale } from './types'
import { translate } from './dictionaries'
import type { TranslationKey } from './dictionaries'

const STORAGE_KEY = 'orchestratord.locale'

export interface I18nContextValue {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: TranslationKey) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function I18nProvider({
  children,
  initialLocale = 'en',
}: {
  children: ReactNode
  initialLocale?: Locale
}) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale)

  // Read a persisted choice only after hydration (SSR always renders the
  // default locale first, avoiding a server/client mismatch).
  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'en' || stored === 'zh-CN') {
      setLocaleState(stored)
    }
  }, [])

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale: (next: Locale) => {
        setLocaleState(next)
        window.localStorage.setItem(STORAGE_KEY, next)
      },
      t: (key: TranslationKey) => translate(locale, key),
    }),
    [locale],
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    throw new Error('useI18n must be used within an I18nProvider')
  }
  return ctx
}

/** Alias for components that only need translation + locale switching. */
export const useTranslation = useI18n

export function useLocale(): Locale {
  return useI18n().locale
}
