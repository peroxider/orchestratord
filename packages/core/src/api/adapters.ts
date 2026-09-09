import type { ResourceRef } from '@orchestratord/app-contracts'
import type { AuditLogEntry, InboxItem, Session } from './types'

type LegacySessionDto = Omit<Session, 'source_ref' | 'origin'> & { source_ref?: ResourceRef | null; issue_id?: string | null }
type LegacyInboxDto = Omit<InboxItem, 'application_id' | 'resource_ref' | 'session_ref'> & { application_id?: string | null; resource_ref?: ResourceRef | null; session_ref?: ResourceRef | null; issue_id?: string | null }
type LegacyAuditDto = Omit<AuditLogEntry, 'target_ref'> & { target_ref?: ResourceRef }

export function adaptSession(dto: LegacySessionDto): Session {
  const source_ref = dto.source_ref ?? (dto.issue_id ? { application_id: 'issue_pr', kind: 'issue', id: dto.issue_id } : null)
  const { issue_id: _legacyIssueId, ...platform } = dto
  return { ...platform, source_ref, origin: source_ref ? { kind: 'resource', source: source_ref } : { kind: 'direct' } }
}

export function adaptInboxItem(dto: LegacyInboxDto): InboxItem {
  const resource_ref = dto.resource_ref ?? (dto.issue_id ? { application_id: 'issue_pr', kind: 'issue', id: dto.issue_id } : null)
  const session_ref = dto.session_ref ?? (dto.session_id ? { application_id: null, kind: 'session', id: dto.session_id } : null)
  const { issue_id: _legacyIssueId, ...platform } = dto
  return { ...platform, application_id: dto.application_id ?? resource_ref?.application_id ?? null, resource_ref, session_ref }
}

export function adaptAuditEntry(dto: LegacyAuditDto): AuditLogEntry {
  return { ...dto, target_ref: dto.target_ref ?? { application_id: dto.target_type === 'issue' ? 'issue_pr' : null, kind: dto.target_type, id: dto.target_id } }
}
