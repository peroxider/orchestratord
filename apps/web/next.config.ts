import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: [
    '@orchestratord/core',
    '@orchestratord/ui',
    '@orchestratord/views',
  ],
}

export default nextConfig
