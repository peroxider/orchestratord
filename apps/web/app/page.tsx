import Link from 'next/link'

export default function HomePage() {
  return (
    <main>
      <h1>orchestratord</h1>
      <p>AI task management platform.</p>
      <Link href="/login">Sign in</Link>
    </main>
  )
}
