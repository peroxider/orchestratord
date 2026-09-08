'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { CoreProvider } from '@orchestratord/core'
import { I18nProvider } from '@orchestratord/views'

/* -------------------------------------------------------------------------- */
/*  ThemeProvider — root-level data-theme controller                          */
/* -------------------------------------------------------------------------- */

const THEME_STORAGE_KEY = 'orchestratord.theme'

export type Theme = 'light' | 'dark'

interface ThemeContextValue {
  theme: Theme
  setTheme: (next: Theme) => void
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

/**
 * Sets `<html data-theme="…">` from a persisted localStorage entry so the
 * rest of the app (globals.css, primitives) can theme via attribute selectors
 * without a separate stylesheet swap. Mirrors the SSR-default + post-hydration
 * read pattern that ``I18nProvider`` uses in
 * ``packages/views/src/i18n/context.tsx:25-56`` to avoid hydration mismatches.
 */
export function ThemeProvider({
  children,
  initialTheme = 'dark',
}: {
  children: ReactNode
  initialTheme?: Theme
}) {
  const [theme, setThemeState] = useState<Theme>(() => {
    if (typeof window === 'undefined') return initialTheme
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : initialTheme
  })

  // Reflect state into the DOM attribute on every change so the design
  // tokens in ``apps/web/app/globals.css`` re-resolve.
  useEffect(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.dataset.theme = theme
    }
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    }
  }, [])

  const toggleTheme = useCallback(() => {
    setThemeState((current) => {
      const next: Theme = current === 'dark' ? 'light' : 'dark'
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(THEME_STORAGE_KEY, next)
      }
      return next
    })
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, setTheme, toggleTheme }),
    [theme, setTheme, toggleTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return ctx
}

/* -------------------------------------------------------------------------- */
/*  WebProviders — root-level composition                                     */
/* -------------------------------------------------------------------------- */

/**
 * Root-level providers mounted in ``apps/web/app/layout.tsx``. Kept thin on
 * purpose: workspace-scoped concerns (TanStack Query, workspace Zustand
 * store, realtime bridge) live inside ``DashboardGuard`` so the marketing
 * and login routes don't pay for them.
 *
 * Order is significant: theme must wrap the tree so tokens resolve on first
 * paint; i18n wraps everything so login + marketing see translations; the
 * auth gate is outermost so a future redirect-on-401 can intercept before
 * any child provider runs its effects.
 */
export function WebProviders({ children }: { children: ReactNode }) {
  return (
    <CoreProvider>
      <ThemeProvider>
        <I18nProvider>{children}</I18nProvider>
      </ThemeProvider>
    </CoreProvider>
  )
}
