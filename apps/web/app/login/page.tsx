'use client'

import { useRouter } from 'next/navigation'
import { useState, type FormEvent } from 'react'

import { apiClient } from '@/lib/api'
import { setToken } from '@/lib/auth'

interface VerifyIdentity {
  workspace_id: string
  workspace_slug: string
  member_id: string
  member_name: string
}

/**
 * Token-paste login. There are no accounts: the credential is an
 * ``auth_tokens`` plaintext (printed once by ``orchestratord serve`` or
 * minted on the tokens page). ``POST /api/auth/verify`` exchanges it for
 * the workspace identity; on success the token is persisted and the user
 * lands in their workspace console.
 */
export default function LoginPage() {
  const router = useRouter()
  const [token, setTokenValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const candidate = token.trim()
    if (!candidate) {
      return
    }
    setPending(true)
    setError(null)
    try {
      const identity = await apiClient.request<VerifyIdentity>(
        '/api/auth/verify',
        { method: 'POST', body: JSON.stringify({ token: candidate }) },
      )
      setToken(candidate)
      router.replace(`/${identity.workspace_slug}`)
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : 'Sign-in failed. Check the token and try again.',
      )
      setPending(false)
    }
  }

  return (
    <main>
      <h1>Sign in</h1>
      <form onSubmit={handleSubmit}>
        <div>
          <label htmlFor="token">Token</label>
          <input
            id="token"
            name="token"
            type="password"
            required
            autoFocus
            autoComplete="off"
            value={token}
            onChange={(e) => setTokenValue(e.target.value)}
          />
        </div>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={pending}>
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}
