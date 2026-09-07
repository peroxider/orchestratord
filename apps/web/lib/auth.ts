/**
 * Bearer-token session store. The console has no accounts — the credential
 * is an ``auth_tokens`` plaintext issued by ``orchestratord serve`` (or the
 * tokens page) and verified via ``POST /api/auth/verify``. It is held in
 * localStorage and attached as ``Authorization: Bearer`` by
 * ``apps/web/lib/api.ts`` / the realtime bridge.
 */
const TOKEN_STORAGE_KEY = 'orchestratord.token'

export function getToken(): string | null {
  if (typeof window === 'undefined') {
    return null
  }
  return window.localStorage.getItem(TOKEN_STORAGE_KEY)
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY)
}
