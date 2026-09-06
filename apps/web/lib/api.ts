import { ApiClient } from '@orchestratord/core'

export const apiClient = new ApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:9000',
})
