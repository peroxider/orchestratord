import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(process.cwd(), '..', '..')
function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap(name => {
    const path = join(directory, name)
    return statSync(path).isDirectory() ? sourceFiles(path) : /\.(ts|tsx)$/.test(name) ? [path] : []
  })
}

describe('frontend dependency boundaries', () => {
  it('keeps platform packages independent from concrete applications', () => {
    const platform = ['packages/ui/src', 'packages/core/src', 'packages/views/src'].flatMap(directory => sourceFiles(join(root, directory)))
    for (const file of platform) expect(readFileSync(file, 'utf8'), file).not.toMatch(/from ['"]@orchestratord\/app-(?!contracts)/)
  })

  it('keeps legacy business fields inside the single API adapter', () => {
    const surfaces = ['apps/web/components', 'packages/views/src', 'packages/core/src'].flatMap(directory => sourceFiles(join(root, directory))).filter(file => !file.endsWith(join('api', 'adapters.ts')))
    for (const file of surfaces) expect(readFileSync(file, 'utf8'), file).not.toMatch(/\.issue_id\b/)
  })
})
