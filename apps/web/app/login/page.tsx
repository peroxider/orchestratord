export default function LoginPage() {
  return (
    <main>
      <h1>Sign in</h1>
      {/* Phase 1 stub — real auth wiring lands with §5.6 (cookie session). */}
      <form method="post">
        <div>
          <label htmlFor="email">Email</label>
          <input id="email" name="email" type="email" required />
        </div>
        <div>
          <label htmlFor="password">Password</label>
          <input id="password" name="password" type="password" required />
        </div>
        <button type="submit">Sign in</button>
      </form>
    </main>
  )
}
