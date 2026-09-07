'use client'

import { useI18n } from './context'
import type { Locale } from './types'

const OPTIONS: Array<{ value: Locale; label: string }> = [
  { value: 'en', label: 'EN' },
  { value: 'zh-CN', label: '中文' },
  { value: 'ja', label: '日本語' },
]

export function LocaleSwitcher() {
  const { locale, setLocale } = useI18n()

  return (
    <div className="locale-switcher" role="group" aria-label="Language">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          className={
            locale === option.value
              ? 'locale-switcher__option locale-switcher__option--active'
              : 'locale-switcher__option'
          }
          aria-pressed={locale === option.value}
          onClick={() => setLocale(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
