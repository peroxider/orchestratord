"""Lookup-vocabulary tests: every §6.1 repository query method (live DB).

Each test seeds the minimal parent/child rows a query targets and asserts the
method returns exactly the expected rows while filtering out others. Rows are
visible within a test without an explicit commit because ``Repository.add``
flushes to the session; commits are used only where a test needs to prove a
``get``/``all`` round-trip through a fresh unit of work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from orchestratord.db.models import (
    Agent,
    AgentCapabilitiesCache,
    Approval,
    AuditLogEntry,
    AuthToken,
    Autopilot,
    AutopilotRun,
    Channel,
    InboxItem,
    Issue,
    IssueComment,
    IssueLabel,
    IssueStatusChange,
    Member,
    MemberAgentScope,
    Project,
    ProjectDoc,
    ProjectRepo,
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
from orchestratord.db.repository import Repositories

pytestmark = pytest.mark.database


def _now() -> datetime:
    return datetime.now(UTC)


async def _add_workspace(repos: Repositories, slug: str = "acme") -> Workspace:
    ws = Workspace(
        id=uuid.uuid4(), slug=slug, name=slug.title(), created_at=_now()
    )
    await repos.workspaces.add(ws)
    return ws


async def _add_agent(
    repos: Repositories, workspace_id, name: str = "gpt"
) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        provider="openai",
        runtime_id=uuid.uuid4(),
        capabilities_cache_jsonb={},
        created_at=_now(),
    )
    await repos.agents.add(agent)
    return agent


async def _add_runtime(
    repos: Repositories, workspace_id, token_hash: str = "hash1"
) -> Runtime:
    rt = Runtime(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        hostname="box",
        os="linux",
        token_hash=token_hash,
        status="online",
        last_seen_at=None,
        created_at=_now(),
    )
    await repos.runtimes.add(rt)
    return rt


# --- Tenancy ---------------------------------------------------------------


async def test_workspace_by_slug(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos, "acme")
    assert (await repos.workspaces.by_slug("acme")).id == ws.id
    assert await repos.workspaces.by_slug("missing") is None


async def test_member_list_for_workspace(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    b = await _add_workspace(repos, "b")
    member = Member(
        id=uuid.uuid4(), workspace_id=a.id, role="owner", name="M", created_at=_now()
    )
    await repos.members.add(member)
    assert [m.id for m in await repos.members.list_for_workspace(a.id)] == [member.id]
    assert await repos.members.list_for_workspace(b.id) == []


async def test_member_agent_scope_lookups(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    member = Member(
        id=uuid.uuid4(), workspace_id=ws.id, role="owner", name="M", created_at=_now()
    )
    agent = await _add_agent(repos, ws.id)
    await repos.members.add(member)
    scope = MemberAgentScope(member_id=member.id, agent_id=agent.id)
    await repos.member_agent_scopes.add(scope)
    assert [
        s.agent_id for s in await repos.member_agent_scopes.list_for_member(member.id)
    ] == [agent.id]
    assert [
        s.member_id for s in await repos.member_agent_scopes.list_for_agent(agent.id)
    ] == [member.id]


# --- Agents / runtimes -----------------------------------------------------


async def test_agent_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    b = await _add_workspace(repos, "b")
    rt = await _add_runtime(repos, a.id)
    agent = Agent(
        id=uuid.uuid4(),
        workspace_id=a.id,
        name="gpt",
        provider="openai",
        runtime_id=rt.id,
        capabilities_cache_jsonb={},
        created_at=_now(),
    )
    await repos.agents.add(agent)
    assert [x.id for x in await repos.agents.list_for_workspace(a.id)] == [agent.id]
    assert await repos.agents.list_for_workspace(b.id) == []
    assert (await repos.agents.by_name(a.id, "gpt")).id == agent.id
    assert await repos.agents.by_name(a.id, "missing") is None
    assert [x.id for x in await repos.agents.list_for_runtime(rt.id)] == [agent.id]


async def test_agent_capabilities_cache_by_agent(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    agent = await _add_agent(repos, ws.id)
    c1 = AgentCapabilitiesCache(
        id=uuid.uuid4(),
        agent_id=agent.id,
        backend_name="claude",
        capabilities_jsonb={},
        version="1",
        model_pricing_jsonb=None,
        refreshed_at=_now(),
    )
    c2 = AgentCapabilitiesCache(
        id=uuid.uuid4(),
        agent_id=agent.id,
        backend_name="codex",
        capabilities_jsonb={},
        version="2",
        model_pricing_jsonb={},
        refreshed_at=_now(),
    )
    await repos.agent_capabilities_cache.add(c1)
    await repos.agent_capabilities_cache.add(c2)
    assert len(await repos.agent_capabilities_cache.by_agent(agent.id)) == 2
    assert [
        c.backend_name
        for c in await repos.agent_capabilities_cache.by_agent(agent.id, "claude")
    ] == ["claude"]


async def test_runtime_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    b = await _add_workspace(repos, "b")
    rt = await _add_runtime(repos, a.id, "hash1")
    assert [x.id for x in await repos.runtimes.list_for_workspace(a.id)] == [rt.id]
    assert await repos.runtimes.list_for_workspace(b.id) == []
    assert (await repos.runtimes.by_token_hash("hash1")).id == rt.id
    assert await repos.runtimes.by_token_hash("missing") is None


async def test_runtime_backend_list_for_runtime(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    rt = await _add_runtime(repos, ws.id)
    rb = RuntimeBackend(
        id=uuid.uuid4(),
        runtime_id=rt.id,
        backend_name="codex",
        version="1.0",
        probed_at=_now(),
    )
    await repos.runtime_backends.add(rb)
    assert [
        x.backend_name for x in await repos.runtime_backends.list_for_runtime(rt.id)
    ] == ["codex"]


# --- Issues ----------------------------------------------------------------


async def test_issue_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    b = await _add_workspace(repos, "b")
    issue = Issue(
        id=uuid.uuid4(),
        workspace_id=a.id,
        title="Bug",
        description="desc",
        status="open",
        assignee_type="agent",
        assignee_id=uuid.uuid4(),
        created_at=_now(),
    )
    await repos.issues.add(issue)
    assert [x.id for x in await repos.issues.list_for_workspace(a.id)] == [issue.id]
    assert await repos.issues.list_for_workspace(b.id) == []
    assert [
        x.id for x in await repos.issues.list_for_assignee("agent", issue.assignee_id)
    ] == [issue.id]
    assert await repos.issues.list_for_assignee("agent", uuid.uuid4()) == []


async def test_issue_comment_list_for_issue(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    issue = Issue(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        title="Bug",
        description="d",
        status="open",
        assignee_type=None,
        assignee_id=None,
        created_at=_now(),
    )
    await repos.issues.add(issue)
    comment = IssueComment(
        id=uuid.uuid4(),
        issue_id=issue.id,
        author_type="agent",
        author_id=uuid.uuid4(),
        body="hi",
        created_at=_now(),
    )
    await repos.issue_comments.add(comment)
    assert [c.id for c in await repos.issue_comments.list_for_issue(issue.id)] == [
        comment.id
    ]
    assert await repos.issue_comments.list_for_issue(uuid.uuid4()) == []


async def test_issue_label_list_for_issue(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    issue = Issue(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        title="Bug",
        description="d",
        status="open",
        assignee_type=None,
        assignee_id=None,
        created_at=_now(),
    )
    await repos.issues.add(issue)
    await repos.issue_labels.add(IssueLabel(issue_id=issue.id, name="bug"))
    await repos.issue_labels.add(IssueLabel(issue_id=issue.id, name="p0"))
    assert {l.name for l in await repos.issue_labels.list_for_issue(issue.id)} == {
        "bug",
        "p0",
    }


async def test_issue_status_change_list_for_issue(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    issue = Issue(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        title="Bug",
        description="d",
        status="open",
        assignee_type=None,
        assignee_id=None,
        created_at=_now(),
    )
    await repos.issues.add(issue)
    change = IssueStatusChange(
        id=uuid.uuid4(),
        issue_id=issue.id,
        from_status=None,
        to_status="open",
        created_at=_now(),
    )
    await repos.issue_status_history.add(change)
    assert [
        c.id for c in await repos.issue_status_history.list_for_issue(issue.id)
    ] == [change.id]


# --- Sessions / runs / approvals -------------------------------------------


async def test_session_list_filters(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    b = await _add_workspace(repos, "b")
    issue = Issue(
        id=uuid.uuid4(),
        workspace_id=a.id,
        title="Bug",
        description="d",
        status="open",
        assignee_type=None,
        assignee_id=None,
        created_at=_now(),
    )
    await repos.issues.add(issue)
    s1 = Session(
        id=uuid.uuid4(),
        workspace_id=a.id,
        issue_id=issue.id,
        agent_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        mode="auto",
        status="running",
        created_at=_now(),
    )
    await repos.sessions.add(s1)
    assert [s.id for s in await repos.sessions.list(workspace_id=a.id)] == [s1.id]
    assert [s.id for s in await repos.sessions.list(issue_id=issue.id)] == [s1.id]
    assert await repos.sessions.list(workspace_id=b.id) == []
    assert await repos.sessions.list(workspace_id=a.id, issue_id=uuid.uuid4()) == []


async def test_run_list_for_workspace(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    run = Run(
        id=uuid.uuid4(),
        workspace_id=a.id,
        kind="issue",
        status="done",
        created_at=_now(),
        finished_at=None,
    )
    await repos.runs.add(run)
    assert [r.id for r in await repos.runs.list_for_workspace(a.id)] == [run.id]


async def test_approval_by_session_request(db) -> None:
    repos = Repositories(db)
    approval = Approval(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        request_id="req-1",
        decision=None,
        created_at=_now(),
        decided_at=None,
    )
    await repos.approvals.add(approval)
    got = await repos.approvals.by_session_request(approval.session_id, "req-1")
    assert got.id == approval.id
    assert (
        await repos.approvals.by_session_request(approval.session_id, "nope") is None
    )


# --- Skills ----------------------------------------------------------------


async def test_skill_by_name(db) -> None:
    repos = Repositories(db)
    skill = Skill(
        id=uuid.uuid4(),
        name="deploy",
        display_name="Deploy",
        description="d",
        user_invocable=True,
        allowed_tools=["git"],
        version=1,
        skill_md_path="/s/deploy.md",
        is_stale=False,
        stale_reasons=[],
        created_at=_now(),
    )
    await repos.skills.add(skill)
    assert (await repos.skills.by_name("deploy")).id == skill.id
    assert await repos.skills.by_name("missing") is None


async def test_skill_source_map_list_for_skill(db) -> None:
    repos = Repositories(db)
    skill = Skill(
        id=uuid.uuid4(),
        name="deploy",
        display_name="Deploy",
        description="d",
        user_invocable=True,
        allowed_tools=[],
        version=1,
        skill_md_path="/s/deploy.md",
        is_stale=False,
        stale_reasons=[],
        created_at=_now(),
    )
    await repos.skills.add(skill)
    sm = SkillSourceMap(
        id=uuid.uuid4(),
        skill_id=skill.id,
        path="/s/deploy.md",
        content_hash="abc",
    )
    await repos.skill_source_maps.add(sm)
    assert [m.id for m in await repos.skill_source_maps.list_for_skill(skill.id)] == [
        sm.id
    ]


async def test_skill_reference_lookups(db) -> None:
    repos = Repositories(db)
    skill = Skill(
        id=uuid.uuid4(),
        name="deploy",
        display_name="Deploy",
        description="d",
        user_invocable=True,
        allowed_tools=[],
        version=1,
        skill_md_path="/s/deploy.md",
        is_stale=False,
        stale_reasons=[],
        created_at=_now(),
    )
    await repos.skills.add(skill)
    sm = SkillSourceMap(
        id=uuid.uuid4(),
        skill_id=skill.id,
        path="/s/deploy.md",
        content_hash="abc",
    )
    await repos.skill_source_maps.add(sm)
    ref = SkillReference(
        id=uuid.uuid4(),
        skill_id=skill.id,
        source_map_id=sm.id,
        file_path="/s/deploy.md",
        start_line=1,
        end_line=2,
        sha_prefix="abc123",
        claim="tool",
    )
    await repos.skill_references.add(ref)
    assert [
        r.id for r in await repos.skill_references.list_for_skill(skill.id)
    ] == [ref.id]
    assert [
        r.id for r in await repos.skill_references.list_for_source_map(sm.id)
    ] == [ref.id]


# --- Inbox / usage ---------------------------------------------------------


async def test_inbox_list_for_workspace(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    item = InboxItem(
        id=uuid.uuid4(),
        workspace_id=a.id,
        kind="issue",
        title="t",
        issue_id=None,
        session_id=None,
        event_seq=None,
        status="new",
        assignee_type=None,
        assignee_id=None,
        created_at=_now(),
    )
    await repos.inbox.add(item)
    assert [i.id for i in await repos.inbox.list_for_workspace(a.id)] == [item.id]


async def test_usage_aggregate_by_bucket_and_list(db) -> None:
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    agent_id = uuid.uuid4()
    day = date(2026, 9, 1)
    agg = UsageAggregate(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        agent_id=agent_id,
        issue_id=None,
        backend="claude",
        day=day,
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.5,
        sessions=1,
    )
    await repos.usage_aggregates.add(agg)
    got = await repos.usage_aggregates.by_bucket(ws.id, agent_id, None, "claude", day)
    assert got.id == agg.id
    assert (
        await repos.usage_aggregates.by_bucket(
            ws.id, agent_id, None, "claude", date(2026, 9, 2)
        )
        is None
    )
    assert [
        u.id for u in await repos.usage_aggregates.list_for_workspace(ws.id)
    ] == [agg.id]


async def test_usage_aggregate_upsert_accumulates_bucket(db) -> None:
    """Two ``upsert`` calls on the same (NULL-inclusive) bucket converge on
    one row with summed metrics — the §0.5 usage-ingestion contract."""
    repos = Repositories(db)
    ws = await _add_workspace(repos)
    day = date(2026, 9, 1)
    await repos.usage_aggregates.upsert(
        workspace_id=ws.id,
        agent_id=None,
        issue_id=None,
        backend="codex",
        day=day,
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.25,
    )
    await repos.usage_aggregates.upsert(
        workspace_id=ws.id,
        agent_id=None,
        issue_id=None,
        backend="codex",
        day=day,
        tokens_in=7,
        tokens_out=3,
        cost_usd=0.5,
    )
    got = await repos.usage_aggregates.by_bucket(ws.id, None, None, "codex", day)
    assert got is not None
    assert (got.tokens_in, got.tokens_out) == (17, 8)
    assert got.cost_usd == 0.75
    assert got.sessions == 2
    assert len(await repos.usage_aggregates.list_for_workspace(ws.id)) == 1


# --- Collaboration ---------------------------------------------------------


async def test_squad_and_member_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    squad = Squad(
        id=uuid.uuid4(),
        workspace_id=a.id,
        name="eng",
        leader_type="member",
        leader_id=uuid.uuid4(),
        is_deleted=False,
        created_at=_now(),
    )
    await repos.squads.add(squad)
    sm = SquadMember(squad_id=squad.id, member_type="member", member_id=uuid.uuid4())
    await repos.squad_members.add(sm)
    assert [s.id for s in await repos.squads.list_for_workspace(a.id)] == [squad.id]
    assert [m.member_id for m in await repos.squad_members.list_for_squad(squad.id)] == [
        sm.member_id
    ]


async def test_project_repo_doc_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    project = Project(
        id=uuid.uuid4(), workspace_id=a.id, name="p", description="d"
    )
    await repos.projects.add(project)
    repo = ProjectRepo(
        id=uuid.uuid4(),
        project_id=project.id,
        repo_url="https://git/x",
        default_branch="main",
    )
    doc = ProjectDoc(
        id=uuid.uuid4(), project_id=project.id, doc_url="https://d", doc_type="readme"
    )
    await repos.project_repos.add(repo)
    await repos.project_docs.add(doc)
    assert [p.id for p in await repos.projects.list_for_workspace(a.id)] == [project.id]
    assert [r.id for r in await repos.project_repos.list_for_project(project.id)] == [
        repo.id
    ]
    assert [d.id for d in await repos.project_docs.list_for_project(project.id)] == [
        doc.id
    ]


async def test_autopilot_and_run_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    ap = Autopilot(
        id=uuid.uuid4(),
        workspace_id=a.id,
        name="nightly",
        cron="0 0 * * *",
        prompt="p",
        target_kind="issue",
        target_id=uuid.uuid4(),
        enabled=True,
    )
    await repos.autopilots.add(ap)
    run = AutopilotRun(
        id=uuid.uuid4(),
        autopilot_id=ap.id,
        scheduled_at=_now(),
        run_id=uuid.uuid4(),
        status="done",
        started_at=None,
        finished_at=None,
    )
    await repos.autopilot_runs.add(run)
    assert [x.id for x in await repos.autopilots.list_for_workspace(a.id)] == [ap.id]
    assert [
        r.id for r in await repos.autopilot_runs.list_for_autopilot(ap.id)
    ] == [run.id]


# --- Audit / auth / channels -----------------------------------------------


async def test_audit_log_list_for_workspace(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    entry = AuditLogEntry(
        id=uuid.uuid4(),
        workspace_id=a.id,
        actor_type="member",
        actor_id="u1",
        action="create",
        target_type="issue",
        target_id="i1",
        payload_jsonb=None,
        created_at=_now(),
    )
    await repos.audit_log.add(entry)
    assert [e.id for e in await repos.audit_log.list_for_workspace(a.id)] == [entry.id]


async def test_auth_token_lookups(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    token = AuthToken(
        id=uuid.uuid4(),
        workspace_id=a.id,
        name="ci",
        token_hash="h1",
        scopes=["read"],
        expires_at=None,
        created_at=_now(),
    )
    await repos.auth_tokens.add(token)
    assert (await repos.auth_tokens.by_token_hash("h1")).id == token.id
    assert await repos.auth_tokens.by_token_hash("missing") is None
    assert [t.id for t in await repos.auth_tokens.list_for_workspace(a.id)] == [
        token.id
    ]


async def test_channel_list_for_workspace(db) -> None:
    repos = Repositories(db)
    a = await _add_workspace(repos, "a")
    ch = Channel(
        id=uuid.uuid4(),
        workspace_id=a.id,
        provider="slack",
        name="general",
        external_id="C123",
        created_at=_now(),
    )
    await repos.channels.add(ch)
    assert [c.id for c in await repos.channels.list_for_workspace(a.id)] == [ch.id]
