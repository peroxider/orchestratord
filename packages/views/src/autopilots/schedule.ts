export function cronSummary(expression: string, locale: 'en' | 'zh-CN' | 'ja' = 'en'): string {
  const c = {
    en: { invalid: 'Invalid schedule', every: (n: number) => `Every ${n} minutes`, hourly: (m: string) => `Hourly at :${m}`, daily: (time: string) => `Daily at ${time}`, custom: 'Custom schedule' },
    'zh-CN': { invalid: '计划格式无效', every: (n: number) => `每 ${n} 分钟`, hourly: (m: string) => `每小时第 ${m} 分钟`, daily: (time: string) => `每天 ${time}`, custom: '自定义计划' },
    ja: { invalid: 'スケジュール形式が無効です', every: (n: number) => `${n} 分ごと`, hourly: (m: string) => `毎時 ${m} 分`, daily: (time: string) => `毎日 ${time}`, custom: 'カスタムスケジュール' },
  }[locale]
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5) return c.invalid
  const [minute, hour, day, month, weekday] = fields
  if (day === '*' && month === '*' && weekday === '*') {
    if (minute?.startsWith('*/') && hour === '*') {
      const interval = Number(minute.slice(2))
      if (Number.isFinite(interval) && interval > 0) return c.every(interval)
    }
    if (/^\d+$/.test(minute ?? '') && hour === '*') return c.hourly(minute!.padStart(2, '0'))
    if (/^\d+$/.test(minute ?? '') && /^\d+$/.test(hour ?? '')) {
      return c.daily(`${hour!.padStart(2, '0')}:${minute!.padStart(2, '0')}`)
    }
  }
  return `${c.custom} · ${expression.trim()}`
}
