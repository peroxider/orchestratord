export interface ApiClientOptions {
  baseUrl: string
  fetchFn?: typeof fetch
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

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '')
    this.fetchFn = options.fetchFn ?? fetch
  }

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const headers = new Headers(init?.headers)
    if (init?.body != null && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
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
