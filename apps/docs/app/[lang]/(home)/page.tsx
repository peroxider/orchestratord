import Link from 'next/link'

export default async function HomePage({
  params,
}: {
  params: Promise<{ lang: string }>
}) {
  const { lang } = await params
  const isZh = lang === 'zh-CN'

  return (
    <main
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        textAlign: 'center',
        gap: '1rem',
        padding: '2rem',
      }}
    >
      <h1 style={{ fontSize: '2.5rem', fontWeight: 700 }}>
        {isZh ? 'orchestratord 文档' : 'orchestratord Documentation'}
      </h1>
      <p
        style={{
          fontSize: '1.125rem',
          color: 'var(--color-fd-muted-foreground)',
          maxWidth: '36rem',
        }}
      >
        {isZh
          ? 'CLI 时代的多智能体编排。统一 SPI、能力矩阵、agent 可调用 skills。'
          : 'Multi-agent orchestration for the CLI era. A unified SPI, a capability matrix, and agent-callable skills.'}
      </p>
      <Link
        href={`/${lang}/docs`}
        style={{ fontWeight: 600, textDecoration: 'underline' }}
      >
        {isZh ? '浏览文档 →' : 'Browse the docs →'}
      </Link>
    </main>
  )
}
