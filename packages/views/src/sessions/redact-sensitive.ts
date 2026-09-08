const SENSITIVE_KEY = /(?:authorization|cookie|credential|password|passwd|secret|api[-_]?key|(?:access|refresh|auth)?[-_]?token)/i
const INLINE_SECRET = /\b(Bearer\s+)[A-Za-z0-9._~+\/-]{8,}|\b(sk-[A-Za-z0-9_-]{8,})|\b((?:token|password|secret|api_?key)\s*[=:]\s*)[^\s,;]+/gi

/** Redact execution evidence before it reaches any expandable UI surface. */
export function redactSensitive(value: unknown): unknown {
  const seen = new WeakSet<object>()
  const visit = (current: unknown): unknown => {
    if (typeof current === 'string') {
      return current.replace(INLINE_SECRET, (match, bearerPrefix, openAiKey, assignmentPrefix) => {
        if (bearerPrefix) return `${bearerPrefix}[REDACTED]`
        if (openAiKey) return '[REDACTED]'
        if (assignmentPrefix) return `${assignmentPrefix}[REDACTED]`
        return match
      })
    }
    if (!current || typeof current !== 'object') return current
    if (seen.has(current)) return '[CIRCULAR]'
    seen.add(current)
    if (Array.isArray(current)) return current.map(visit)
    return Object.fromEntries(Object.entries(current as Record<string, unknown>).map(([key, nested]) => [key, SENSITIVE_KEY.test(key) ? '[REDACTED]' : visit(nested)]))
  }
  return visit(value)
}
