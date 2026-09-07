export interface ApiClientOptions {
  baseUrl: string
  fetchFn?: typeof fetch
  /**
   * Returns the bearer token to attach as ``Authorization`` on every
   * request, or ``null`` when unauthenticated (the login page itself).
   * Called per request so a login/logout is picked up immediately.
   */
  getAccessToken?: () => string | null
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly fetchFn: typeof fetch
  private readonly getAccessToken?: () => string | null

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '')
    this.fetchFn = options.fetchFn ?? fetch
    this.getAccessToken = options.getAccessToken
  }

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const headers = new Headers(init?.headers)
    if (init?.body != null && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    const token = this.getAccessToken?.()
    if (token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    const res = await this.fetchFn(url, { ...init, headers })
    if (!res.ok) {
      throw new ApiError(res.status, await readErrorDetail(res))
    }
    if (res.status === 204) {
      return undefined as T
    }
    return (await res.json()) as T
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') {
      return body.detail
    }
  } catch {
    // ignore parse failures and fall back to statusText
  }
  return res.statusText
}
