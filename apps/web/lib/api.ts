import { ApiClient } from '@orchestratord/core'

import { getToken } from '@/lib/auth'

export const apiClient = new ApiClient({
  baseUrl: process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:9000',
  getAccessToken: getToken,
})
