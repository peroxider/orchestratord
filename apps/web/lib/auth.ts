/**
 * Bearer-token session store. The console has no accounts — the credential
 * is an ``auth_tokens`` plaintext issued by ``orchestratord serve`` (or the
 * tokens page) and verified via ``POST /api/auth/verify``. It is held in
 * localStorage and attached as ``Authorization: Bearer`` by
 * ``apps/web/lib/api.ts`` / the realtime bridge.
 *
 * The whole login/multi-user flow is decoupled behind one build-time flag
 * (mirroring the backend's ``ORCHESTRATORD_AUTH``): the default local
 * single-user deployment ships it inert — ``isAuthEnabled()`` is false, no
 * token is requested or stored, and every API/WS call goes out anonymous.
 * Set ``NEXT_PUBLIC_ORCHESTRATORD_AUTH=1`` at build time to re-enable the
 * gated flow for a multi-user deployment.
 */
const TOKEN_STORAGE_KEY = 'orchestratord.token'

export function isAuthEnabled(): boolean {
  return process.env.NEXT_PUBLIC_ORCHESTRATORD_AUTH === '1'
}

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
