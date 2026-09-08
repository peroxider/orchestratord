'use client'

import { useEffect } from 'react'
import { Button } from '@orchestratord/ui'

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  useEffect(() => {
    console.error(error)
  }, [error])

  return (
    <section className="page-state page-state--error" role="alert">
      <strong>This view could not be loaded</strong>
      <p>
        The local data is unchanged. Retry the view; if it fails again, check
        that the orchestratord API is running on this machine.
      </p>
      <Button onClick={reset}>Retry view</Button>
    </section>
  )
}
