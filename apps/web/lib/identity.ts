/**
 * Phase-2 stub identity — a fixed dev member until §5.7.4 cookie-session auth
 * provides the real authenticated member. The comment/mention endpoints
 * require an author (`author_type` + `author_id`), so we use a stable,
 * clearly-placeholder UUID in the meantime.
 */
export const DEV_MEMBER_ID = '00000000-0000-0000-0000-000000000001'

export const devAuthor = {
  author_type: 'member',
  author_id: DEV_MEMBER_ID,
} as const
