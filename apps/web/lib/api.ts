import { ApiClient } from '@orchestratord/core'

export const apiClient = new ApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_URL ?? 'http://127.0.0.1:9000',
})
