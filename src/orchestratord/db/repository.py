"""Typed async repositories over the §6.1 ORM models.

Each repository wraps a single :class:`AsyncSession` and exposes the CRUD plus
the lookup vocabulary implied by the §6.1 index inventory (e.g. ``by_slug`` on
workspaces, ``list_for_workspace`` on members). Repositories operate on the ORM
models directly — mapping to/from the DB-agnostic :mod:`orchestratord.domain`
entities is the router-rewiring increment's job, since those entities carry
in-memory side effects in ``__post_init__``.

:class:`Repositories` is the single facade: construct it with a session and
reach any repository by name.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestratord.db.base import Base
from orchestratord.db.models import (
    Agent,
    AgentCapabilitiesCache,
    Approval,
    AuditLogEntry,
    AuthToken,
    Autopilot,
    AutopilotRun,
    Channel,
    Event,
    GitHubInstallation,
    InboxItem,
    Integration,
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
    Member,
    MemberAgentScope,
    Message,
    Project,
    ProjectDoc,
    ProjectRepo,
    PullRequest,
    Run,
    Runtime,
    RuntimeBackend,
    Session,
    Skill,
    SkillReference,
    SkillSourceMap,
    Squad,
    SquadMember,
    UsageAggregate,
    Workspace,
)
from orchestratord.db.partitions import ensure_monthly_partition

M = TypeVar("M", bound=Base)


class Repository(Generic[M]):
    """Async CRUD for one §6.1 model.

    ``get`` assumes a single-column ``id`` primary key; composite-PK models
    (``member_agent_scopes``, ``issue_labels``, ``squad_members``) use their
    ``list_*`` methods instead.
    """

    model: type[M]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, obj: M) -> M:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, ident: UUID) -> M | None:
        return await self.session.get(self.model, ident)

    async def all(self) -> list[M]:
        result = await self.session.execute(select(self.model))
        return list(result.scalars().all())

    async def delete(self, obj: M) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def count(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(self.model)
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class WorkspaceRepository(Repository[Workspace]):
    model = Workspace

    async def by_slug(self, slug: str) -> Workspace | None:
        result = await self.session.execute(
            select(Workspace).where(Workspace.slug == slug)
        )
        return result.scalar_one_or_none()


class MemberRepository(Repository[Member]):
    model = Member

    async def list_for_workspace(self, workspace_id: UUID) -> list[Member]:
        result = await self.session.execute(
            select(Member).where(Member.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def by_name(self, workspace_id: UUID, name: str) -> Member | None:
        result = await self.session.execute(
            select(Member).where(
                Member.workspace_id == workspace_id, Member.name == name
            )
        )
        return result.scalar_one_or_none()


class MemberAgentScopeRepository(Repository[MemberAgentScope]):
    model = MemberAgentScope

    async def list_for_member(self, member_id: UUID) -> list[MemberAgentScope]:
        result = await self.session.execute(
            select(MemberAgentScope).where(MemberAgentScope.member_id == member_id)
        )
        return list(result.scalars().all())

    async def list_for_agent(self, agent_id: UUID) -> list[MemberAgentScope]:
        result = await self.session.execute(
            select(MemberAgentScope).where(MemberAgentScope.agent_id == agent_id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Agents / runtimes
# ---------------------------------------------------------------------------


class AgentRepository(Repository[Agent]):
    model = Agent

    async def list_for_workspace(self, workspace_id: UUID) -> list[Agent]:
        result = await self.session.execute(
            select(Agent).where(Agent.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def by_name(self, workspace_id: UUID, name: str) -> Agent | None:
        result = await self.session.execute(
            select(Agent).where(
                Agent.workspace_id == workspace_id, Agent.name == name
            )
        )
        return result.scalar_one_or_none()

    async def list_for_runtime(self, runtime_id: UUID) -> list[Agent]:
        result = await self.session.execute(
            select(Agent).where(Agent.runtime_id == runtime_id)
        )
        return list(result.scalars().all())


class AgentCapabilitiesCacheRepository(Repository[AgentCapabilitiesCache]):
    model = AgentCapabilitiesCache

    async def by_agent(
        self, agent_id: UUID, backend_name: str | None = None
    ) -> list[AgentCapabilitiesCache]:
        stmt = select(AgentCapabilitiesCache).where(
            AgentCapabilitiesCache.agent_id == agent_id
        )
        if backend_name is not None:
            stmt = stmt.where(
                AgentCapabilitiesCache.backend_name == backend_name
            )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class RuntimeRepository(Repository[Runtime]):
    model = Runtime

    async def list_for_workspace(self, workspace_id: UUID) -> list[Runtime]:
        result = await self.session.execute(
            select(Runtime).where(Runtime.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def by_token_hash(self, token_hash: str) -> Runtime | None:
        result = await self.session.execute(
            select(Runtime).where(Runtime.token_hash == token_hash)
        )
        return result.scalar_one_or_none()


class RuntimeBackendRepository(Repository[RuntimeBackend]):
    model = RuntimeBackend

    async def list_for_runtime(self, runtime_id: UUID) -> list[RuntimeBackend]:
        result = await self.session.execute(
            select(RuntimeBackend).where(RuntimeBackend.runtime_id == runtime_id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


class IssueRepository(Repository[Issue]):
    model = Issue

    async def list_for_workspace(self, workspace_id: UUID) -> list[Issue]:
        result = await self.session.execute(
            select(Issue).where(Issue.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def list_for_assignee(
        self, assignee_type: str, assignee_id: UUID
    ) -> list[Issue]:
        result = await self.session.execute(
            select(Issue).where(
                Issue.assignee_type == assignee_type,
                Issue.assignee_id == assignee_id,
            )
        )
        return list(result.scalars().all())


class IssueCommentRepository(Repository[IssueComment]):
    model = IssueComment

    async def list_for_issue(self, issue_id: UUID) -> list[IssueComment]:
        result = await self.session.execute(
            select(IssueComment).where(IssueComment.issue_id == issue_id)
        )
        return list(result.scalars().all())


class IssueLabelRepository(Repository[IssueLabel]):
    model = IssueLabel

    async def list_for_issue(self, issue_id: UUID) -> list[IssueLabel]:
        result = await self.session.execute(
            select(IssueLabel).where(IssueLabel.issue_id == issue_id)
        )
        return list(result.scalars().all())


class IssueStatusChangeRepository(Repository[IssueStatusChange]):
    model = IssueStatusChange

    async def list_for_issue(self, issue_id: UUID) -> list[IssueStatusChange]:
        result = await self.session.execute(
            select(IssueStatusChange).where(
                IssueStatusChange.issue_id == issue_id
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Sessions / runs / events / approvals
# ---------------------------------------------------------------------------


class SessionRepository(Repository[Session]):
    model = Session

    async def list(
        self,
        workspace_id: UUID | None = None,
        issue_id: UUID | None = None,
        agent_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> list[Session]:
        stmt = select(Session)
        if workspace_id is not None:
            stmt = stmt.where(Session.workspace_id == workspace_id)
        if issue_id is not None:
            stmt = stmt.where(Session.issue_id == issue_id)
        if agent_id is not None:
            stmt = stmt.where(Session.agent_id == agent_id)
        if run_id is not None:
            stmt = stmt.where(Session.run_id == run_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class RunRepository(Repository[Run]):
    model = Run

    async def list_for_workspace(self, workspace_id: UUID) -> list[Run]:
        result = await self.session.execute(
            select(Run).where(Run.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class EventRepository(Repository[Event]):
    model = Event

    async def append(self, event: Event) -> Event:
        await ensure_monthly_partition(self.session, event.created_at)
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_for_session(
        self,
        session_id: UUID,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> list[Event]:
        stmt = (
            select(Event)
            .where(Event.session_id == session_id)
            .order_by(Event.sequence)
        )
        if from_seq is not None:
            stmt = stmt.where(Event.sequence >= from_seq)
        if to_seq is not None:
            stmt = stmt.where(Event.sequence <= to_seq)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_workspace(self, workspace_id: UUID) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.workspace_id == workspace_id)
            .order_by(Event.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_for_issue(self, issue_id: UUID) -> list[Event]:
        result = await self.session.execute(
            select(Event)
            .where(Event.issue_id == issue_id)
            .order_by(Event.created_at.desc())
        )
        return list(result.scalars().all())


class ApprovalRepository(Repository[Approval]):
    model = Approval

    async def by_session_request(
        self, session_id: UUID, request_id: str
    ) -> Approval | None:
        result = await self.session.execute(
            select(Approval).where(
                Approval.session_id == session_id,
                Approval.request_id == request_id,
            )
        )
        return result.scalar_one_or_none()


class MessageRepository(Repository[Message]):
    """Per-session chat messages (§6.1).

    ``append`` auto-assigns the per-session ``seq`` by reading
    ``COALESCE(MAX(seq), -1) + 1`` for the target ``session_id`` so
    concurrent appends from the runner don't interleave with user POSTs.
    Callers that already set ``seq`` (e.g. event-fanout writers) can pass it
    through unchanged.
    """

    model = Message

    async def append(
        self,
        message: Message,
        *,
        auto_seq: bool = True,
    ) -> Message:
        if auto_seq and message.seq == 0:
            # Serialize concurrent appends per session: lock the parent
            # row before reading MAX. Without the lock, two interleaved
            # transactions (user POST vs runner bridge flush) both read
            # the same MAX and land duplicate seqs — silently breaking
            # the ``after_seq`` tail-fetch contract (§6.1a).
            await self.session.execute(
                select(Session)
                .where(Session.id == message.session_id)
                .with_for_update()
            )
            stmt = select(func.coalesce(func.max(Message.seq), -1)).where(
                Message.session_id == message.session_id
            )
            result = await self.session.execute(stmt)
            next_seq = int(result.scalar_one()) + 1
            message.seq = next_seq
        self.session.add(message)
        await self.session.flush()
        return message

    async def list_for_session(
        self,
        session_id: UUID,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq)
        )
        if after_seq is not None:
            stmt = stmt.where(Message.seq > after_seq)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class SkillRepository(Repository[Skill]):
    model = Skill

    async def by_name(self, name: str) -> Skill | None:
        result = await self.session.execute(
            select(Skill).where(Skill.name == name)
        )
        return result.scalar_one_or_none()


class SkillSourceMapRepository(Repository[SkillSourceMap]):
    model = SkillSourceMap

    async def list_for_skill(self, skill_id: UUID) -> list[SkillSourceMap]:
        result = await self.session.execute(
            select(SkillSourceMap).where(SkillSourceMap.skill_id == skill_id)
        )
        return list(result.scalars().all())


class SkillReferenceRepository(Repository[SkillReference]):
    model = SkillReference

    async def list_for_skill(self, skill_id: UUID) -> list[SkillReference]:
        result = await self.session.execute(
            select(SkillReference).where(SkillReference.skill_id == skill_id)
        )
        return list(result.scalars().all())

    async def list_for_source_map(
        self, source_map_id: UUID
    ) -> list[SkillReference]:
        result = await self.session.execute(
            select(SkillReference).where(
                SkillReference.source_map_id == source_map_id
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Inbox / usage
# ---------------------------------------------------------------------------


class InboxItemRepository(Repository[InboxItem]):
    model = InboxItem

    async def list_for_workspace(self, workspace_id: UUID) -> list[InboxItem]:
        result = await self.session.execute(
            select(InboxItem).where(InboxItem.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class UsageAggregateRepository(Repository[UsageAggregate]):
    model = UsageAggregate

    async def by_bucket(
        self,
        workspace_id: UUID,
        agent_id: UUID | None,
        issue_id: UUID | None,
        backend: str,
        day: date,
    ) -> UsageAggregate | None:
        result = await self.session.execute(
            select(UsageAggregate).where(
                UsageAggregate.workspace_id == workspace_id,
                UsageAggregate.agent_id.is_not_distinct_from(agent_id),
                UsageAggregate.issue_id.is_not_distinct_from(issue_id),
                UsageAggregate.backend == backend,
                UsageAggregate.day == day,
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        *,
        workspace_id: UUID,
        agent_id: UUID | None,
        issue_id: UUID | None,
        backend: str,
        day: date,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        """Atomically accumulate one session into a daily usage bucket.

        ``INSERT ... ON CONFLICT DO UPDATE`` against the
        ``uq_usage_aggregates_bucket`` unique index (``NULLS NOT DISTINCT``,
        migration 0008) so concurrent completions of the same
        ``(workspace, agent, issue, backend, day)`` bucket converge on one
        row instead of racing a read-then-write.
        """
        insert_stmt = pg_insert(UsageAggregate).values(
            id=uuid4(),
            workspace_id=workspace_id,
            agent_id=agent_id,
            issue_id=issue_id,
            backend=backend,
            day=day,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            sessions=1,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["workspace_id", "agent_id", "issue_id", "backend", "day"],
            set_={
                "tokens_in": UsageAggregate.tokens_in
                + insert_stmt.excluded.tokens_in,
                "tokens_out": UsageAggregate.tokens_out
                + insert_stmt.excluded.tokens_out,
                "cost_usd": UsageAggregate.cost_usd
                + insert_stmt.excluded.cost_usd,
                "sessions": UsageAggregate.sessions + 1,
            },
        )
        await self.session.execute(stmt)

    async def list_for_workspace(
        self, workspace_id: UUID
    ) -> list[UsageAggregate]:
        result = await self.session.execute(
            select(UsageAggregate).where(
                UsageAggregate.workspace_id == workspace_id
            )
        )
        return list(result.scalars().all())

    async def list_for_agent(self, agent_id: UUID) -> list[UsageAggregate]:
        result = await self.session.execute(
            select(UsageAggregate).where(UsageAggregate.agent_id == agent_id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Collaboration
# ---------------------------------------------------------------------------


class SquadRepository(Repository[Squad]):
    model = Squad

    async def list_for_workspace(self, workspace_id: UUID) -> list[Squad]:
        result = await self.session.execute(
            select(Squad).where(Squad.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class SquadMemberRepository(Repository[SquadMember]):
    model = SquadMember

    async def list_for_squad(self, squad_id: UUID) -> list[SquadMember]:
        result = await self.session.execute(
            select(SquadMember).where(SquadMember.squad_id == squad_id)
        )
        return list(result.scalars().all())


class ProjectRepository(Repository[Project]):
    model = Project

    async def list_for_workspace(self, workspace_id: UUID) -> list[Project]:
        result = await self.session.execute(
            select(Project).where(Project.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class ProjectRepoRepository(Repository[ProjectRepo]):
    model = ProjectRepo

    async def list_for_project(self, project_id: UUID) -> list[ProjectRepo]:
        result = await self.session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == project_id)
        )
        return list(result.scalars().all())


class ProjectDocRepository(Repository[ProjectDoc]):
    model = ProjectDoc

    async def list_for_project(self, project_id: UUID) -> list[ProjectDoc]:
        result = await self.session.execute(
            select(ProjectDoc).where(ProjectDoc.project_id == project_id)
        )
        return list(result.scalars().all())


class AutopilotRepository(Repository[Autopilot]):
    model = Autopilot

    async def list_for_workspace(self, workspace_id: UUID) -> list[Autopilot]:
        result = await self.session.execute(
            select(Autopilot).where(Autopilot.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class AutopilotRunRepository(Repository[AutopilotRun]):
    model = AutopilotRun

    async def list_for_autopilot(
        self, autopilot_id: UUID
    ) -> list[AutopilotRun]:
        result = await self.session.execute(
            select(AutopilotRun).where(AutopilotRun.autopilot_id == autopilot_id)
        )
        return list(result.scalars().all())

    async def find_for_slot(
        self, autopilot_id: UUID, scheduled_at: datetime
    ) -> AutopilotRun | None:
        """Run row already recorded for this cron slot (§7.1 dedup)."""
        result = await self.session.execute(
            select(AutopilotRun)
            .where(AutopilotRun.autopilot_id == autopilot_id)
            .where(AutopilotRun.scheduled_at == scheduled_at)
            .limit(1)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Audit / auth / channels
# ---------------------------------------------------------------------------


class AuditLogEntryRepository(Repository[AuditLogEntry]):
    model = AuditLogEntry

    async def list_for_workspace(
        self, workspace_id: UUID
    ) -> list[AuditLogEntry]:
        result = await self.session.execute(
            select(AuditLogEntry)
            .where(AuditLogEntry.workspace_id == workspace_id)
            .order_by(AuditLogEntry.created_at.desc())
        )
        return list(result.scalars().all())


class AuthTokenRepository(Repository[AuthToken]):
    model = AuthToken

    async def by_token_hash(self, token_hash: str) -> AuthToken | None:
        result = await self.session.execute(
            select(AuthToken).where(AuthToken.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def list_for_workspace(self, workspace_id: UUID) -> list[AuthToken]:
        result = await self.session.execute(
            select(AuthToken).where(AuthToken.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


class ChannelRepository(Repository[Channel]):
    model = Channel

    async def list_for_workspace(self, workspace_id: UUID) -> list[Channel]:
        result = await self.session.execute(
            select(Channel).where(Channel.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def by_external_id(self, external_id: str) -> Channel | None:
        """Reverse lookup used by the inbound webhook (§6.4, single-user mode)."""
        result = await self.session.execute(
            select(Channel).where(Channel.external_id == external_id)
        )
        return result.scalars().first()


class IntegrationRepository(Repository[Integration]):
    model = Integration

    async def by_workspace_provider(
        self, workspace_id: UUID, provider: str
    ) -> Integration | None:
        result = await self.session.execute(
            select(Integration).where(
                Integration.workspace_id == workspace_id,
                Integration.provider == provider,
            )
        )
        return result.scalars().first()

    async def list_for_workspace(self, workspace_id: UUID) -> list[Integration]:
        result = await self.session.execute(
            select(Integration).where(Integration.workspace_id == workspace_id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# VCS (§6.5)
# ---------------------------------------------------------------------------


class GitHubInstallationRepository(Repository[GitHubInstallation]):
    model = GitHubInstallation

    async def by_installation_id(
        self, installation_id: int
    ) -> GitHubInstallation | None:
        result = await self.session.execute(
            select(GitHubInstallation).where(
                GitHubInstallation.installation_id == installation_id
            )
        )
        return result.scalars().first()

    async def list_for_workspace(
        self, workspace_id: UUID
    ) -> list[GitHubInstallation]:
        result = await self.session.execute(
            select(GitHubInstallation).where(
                GitHubInstallation.workspace_id == workspace_id
            )
        )
        return list(result.scalars().all())


class PullRequestRepository(Repository[PullRequest]):
    model = PullRequest

    async def by_repo_number(self, repo: str, number: int) -> PullRequest | None:
        result = await self.session.execute(
            select(PullRequest).where(
                PullRequest.repo == repo,
                PullRequest.number == number,
            )
        )
        return result.scalars().first()

    async def list_for_issue(self, issue_id: UUID) -> list[PullRequest]:
        result = await self.session.execute(
            select(PullRequest).where(PullRequest.issue_id == issue_id)
        )
        return list(result.scalars().all())

    async def upsert(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        state: str,
        head_sha: str,
        status: str = "pending",
    ) -> None:
        """Atomically insert or update a PR keyed by ``(repo, number)``.

        Uses PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent
        webhook deliveries for the same PR converge on a single row instead of
        racing (the §6.5 webhook must be idempotent under redelivery). The
        conflict target relies on the inline ``uq_pull_requests_repo_number``
        unique constraint (migration 0040 in production).
        """
        now = datetime.now(UTC)
        stmt = (
            pg_insert(PullRequest)
            .values(
                id=uuid4(),
                issue_id=None,
                repo=repo,
                number=number,
                title=title,
                state=state,
                head_sha=head_sha,
                status=status,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["repo", "number"],
                set_={
                    "title": title,
                    "state": state,
                    "head_sha": head_sha,
                    "updated_at": now,
                },
            )
        )
        await self.session.execute(stmt)


class Repositories:
    """Bundle every repository over a single session (the §6.1 unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        # Exposed for satellite layers that deliberately live outside the
        # aggregate (the peer registry, D5); CRUD routers stay on the
        # per-entity repositories and never commit.
        self.session = session
        self.workspaces = WorkspaceRepository(session)
        self.members = MemberRepository(session)
        self.member_agent_scopes = MemberAgentScopeRepository(session)
        self.agents = AgentRepository(session)
        self.agent_capabilities_cache = AgentCapabilitiesCacheRepository(session)
        self.runtimes = RuntimeRepository(session)
        self.runtime_backends = RuntimeBackendRepository(session)
        self.issues = IssueRepository(session)
        self.issue_comments = IssueCommentRepository(session)
        self.issue_labels = IssueLabelRepository(session)
        self.issue_status_history = IssueStatusChangeRepository(session)
        self.sessions = SessionRepository(session)
        self.runs = RunRepository(session)
        self.events = EventRepository(session)
        self.approvals = ApprovalRepository(session)
        self.messages = MessageRepository(session)
        self.skills = SkillRepository(session)
        self.skill_source_maps = SkillSourceMapRepository(session)
        self.skill_references = SkillReferenceRepository(session)
        self.inbox = InboxItemRepository(session)
        self.usage_aggregates = UsageAggregateRepository(session)
        self.squads = SquadRepository(session)
        self.squad_members = SquadMemberRepository(session)
        self.projects = ProjectRepository(session)
        self.project_repos = ProjectRepoRepository(session)
        self.project_docs = ProjectDocRepository(session)
        self.autopilots = AutopilotRepository(session)
        self.autopilot_runs = AutopilotRunRepository(session)
        self.audit_log = AuditLogEntryRepository(session)
        self.auth_tokens = AuthTokenRepository(session)
        self.channels = ChannelRepository(session)
        self.integrations = IntegrationRepository(session)
        self.installations = GitHubInstallationRepository(session)
        self.pull_requests = PullRequestRepository(session)
