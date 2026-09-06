'use client'

import { useState } from 'react'
import {
  useCreateMember,
  useDeleteMember,
  useMembers,
  useUpdateMember,
  type ApiClient,
  type Member,
  type MemberRole,
} from '@orchestratord/core'
import { Badge, Button, Card, Input } from '@orchestratord/ui'
import { useTranslation } from '../i18n'
import { MEMBER_ROLES, MEMBER_ROLE_TONE, memberRoleLabel } from './member-roles'

export interface MembersListProps {
  client: ApiClient
  workspaceId: string
}

export function MembersList({ client, workspaceId }: MembersListProps) {
  const [name, setName] = useState('')
  const [role, setRole] = useState<MemberRole>('member')
  const { locale } = useTranslation()

  const { data, isPending, isError, error } = useMembers(client, workspaceId)
  const create = useCreateMember(client, workspaceId)

  function submitCreate() {
    create.mutate({ role, name: name.trim() }, { onSuccess: () => setName('') })
  }

  return (
    <div className="members">
      <Card className="members__create">
        <div className="members__create-form">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Member name"
            aria-label="Member name"
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as MemberRole)}
            aria-label="Role"
          >
            {MEMBER_ROLES.map((r) => (
              <option key={r} value={r}>
                {memberRoleLabel(r, locale)}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="primary"
            disabled={create.isPending}
            onClick={submitCreate}
          >
            Add
          </Button>
        </div>
      </Card>

      {isPending ? (
        <p className="members__empty">Loading members…</p>
      ) : isError ? (
        <p className="members__empty">
          Failed to load members: {error?.message ?? 'unknown error'}
        </p>
      ) : (data ?? []).length === 0 ? (
        <p className="members__empty">No members yet.</p>
      ) : (
        <div className="members__grid">
          {(data ?? []).map((member) => (
            <MemberCard
              key={member.id}
              member={member}
              workspaceId={workspaceId}
              client={client}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function MemberCard({
  member,
  workspaceId,
  client,
}: {
  member: Member
  workspaceId: string
  client: ApiClient
}) {
  const [role, setRole] = useState<MemberRole>(member.role)
  const update = useUpdateMember(client, workspaceId, member.id)
  const remove = useDeleteMember(client, workspaceId, member.id)
  const { locale } = useTranslation()

  return (
    <Card className="member-card">
      <header className="member-card__header">
        <span className="member-card__name">{member.name || '(unnamed)'}</span>
        <Badge tone={MEMBER_ROLE_TONE[member.role]}>
          {memberRoleLabel(member.role, locale)}
        </Badge>
      </header>
      <div className="member-card__actions">
        <select
          value={role}
          onChange={(e) => setRole(e.target.value as MemberRole)}
          aria-label="Change role"
        >
          {MEMBER_ROLES.map((r) => (
            <option key={r} value={r}>
              {memberRoleLabel(r, locale)}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          variant="secondary"
          disabled={update.isPending || role === member.role}
          onClick={() => update.mutate({ role })}
        >
          Update
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          Remove
        </Button>
      </div>
    </Card>
  )
}
