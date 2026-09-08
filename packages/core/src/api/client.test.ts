import { describe, expect, it, vi } from 'vitest'
import { ApiClient } from './client'

describe('ApiClient', () => {
  it('parses JSON and leaves Content-Type unset without a body', async () => {
    const fetchFn = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const client = new ApiClient({ baseUrl: 'http://test/', fetchFn })
    const res = await client.request<{ ok: boolean }>('/api/issues')
    expect(res.ok).toBe(true)
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://test/api/issues')
    expect(new Headers(init.headers).get('Content-Type')).toBeNull()
  })

  it('strips trailing slash and returns undefined for 204', async () => {
    const fetchFn = vi.fn(async () => new Response(null, { status: 204 }))
    const client = new ApiClient({ baseUrl: 'http://test/', fetchFn })
    const res = await client.request<void>('/api/issues')
    expect(res).toBeUndefined()
    const [url] = fetchFn.mock.calls[0] as unknown as [string]
    expect(url).toBe('http://test/api/issues')
  })

  it('throws ApiError on 404', async () => {
    const fetchFn = vi.fn(async () =>
      new Response(JSON.stringify({ detail: 'not found' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const client = new ApiClient({ baseUrl: 'http://test', fetchFn })
    await expect(client.request('/api/missing')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'not found',
    })
  })

  it('falls back to statusText when body has no detail', async () => {
    const fetchFn = vi.fn(async () =>
      new Response('oops', { status: 500, statusText: 'Server Error' }),
    )
    const client = new ApiClient({ baseUrl: 'http://test', fetchFn })
    await expect(client.request('/api/broken')).rejects.toMatchObject({
      status: 500,
      message: 'Server Error',
    })
  })
})
