import { describe, expect, it } from 'vitest'
import { ISSUE_STATUSES, STATUS_TONE, issueStatusLabel } from './views/issues/status'
import { PR_STATE_TONE, prStateLabel, prStateTone } from './views/vcs/pr-labels'

describe('Issue → PR visual mappings', () => {
  it('covers every issue status in workflow order', () => {
    expect(ISSUE_STATUSES).toEqual(['queued', 'pending', 'running', 'pending_review', 'completed', 'failed', 'abandoned', 'verification_failed'])
    for (const status of ISSUE_STATUSES) expect(STATUS_TONE[status]).toBeTruthy()
  })

  it('localizes issue states and falls back for future PR states', () => {
    expect(issueStatusLabel('queued', 'zh-CN')).toBe('排队中')
    expect(PR_STATE_TONE.open).toBe('good')
    expect(prStateLabel('merged')).toBe('Merged')
    expect(prStateTone('unknown')).toBe('neutral')
  })
})
