"""Usage REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.4).

Token / cost aggregation surface. The worker consumes ``SESSION_COMPLETE``
events and posts one :class:`UsageRecord` per session; each record is
pre-aggregated into the ``usage_aggregates`` table at the
``(workspace_id, agent_id, issue_id, backend, day)`` bucket granularity. The
Web client reads workspace-scoped (or single-agent) aggregates grouped by
agent / issue / backend / day, summing the pre-aggregated rows. ``from`` /
``to`` bound the window at day granularity; ``group_by`` is validated by the
domain aggregator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.usage import (
    UsageRecord,
    aggregate_usage_rows,
    usage_totals_rows,
)

router = APIRouter(tags=["usage"])


def _record_payload(record: UsageRecord) -> dict:
    return {
        "workspace_id": str(record.workspace_id),
        "agent_id": str(record.agent_id) if record.agent_id else None,
        "issue_id": str(record.issue_id) if record.issue_id else None,
        "backend": record.backend,
        "tokens_in": record.tokens_in,
        "tokens_out": record.tokens_out,
        "tokens_total": record.tokens_total,
        "cost_usd": record.cost_usd,
        "recorded_at": record.recorded_at.isoformat(),
    }


def _filter_window(rows, from_, to):
    if from_ is not None:
        if from_.tzinfo is None:
            from_ = from_.replace(tzinfo=UTC)
        rows = [r for r in rows if r.day >= from_.date()]
    if to is not None:
        if to.tzinfo is None:
            to = to.replace(tzinfo=UTC)
        rows = [r for r in rows if r.day <= to.date()]
    return rows


class _UsageCreate(BaseModel):
    tokens_in: int
    tokens_out: int
    cost_usd: float = 0.0
    agent_id: UUID | None = None
    issue_id: UUID | None = None
    backend: str = ""
    recorded_at: datetime | None = None


@router.post("/api/workspaces/{workspace_id}/usage", status_code=201)
async def record_usage(
    workspace_id: UUID,
    body: _UsageCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    try:
        record = UsageRecord(
            workspace_id=workspace_id,
            tokens_in=body.tokens_in,
            tokens_out=body.tokens_out,
            cost_usd=body.cost_usd,
            agent_id=body.agent_id,
            issue_id=body.issue_id,
            backend=body.backend,
            recorded_at=body.recorded_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = await repos.usage_aggregates.by_bucket(
        workspace_id, record.agent_id, record.issue_id, record.backend, record.day
    )
    if existing is None:
        await repos.usage_aggregates.add(
            orm.UsageAggregate(
                id=uuid4(),
                workspace_id=workspace_id,
                agent_id=record.agent_id,
                issue_id=record.issue_id,
                backend=record.backend,
                day=record.day,
                tokens_in=record.tokens_in,
                tokens_out=record.tokens_out,
                cost_usd=record.cost_usd,
                sessions=1,
            )
        )
    else:
        existing.tokens_in += record.tokens_in
        existing.tokens_out += record.tokens_out
        existing.cost_usd += record.cost_usd
        existing.sessions += 1
    return _record_payload(record)


@router.get("/api/workspaces/{workspace_id}/usage")
async def list_workspace_usage(
    workspace_id: UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    group_by: Annotated[str, Query()] = "agent",
    repos: Repositories = Depends(get_repositories),
) -> dict:
    rows = await repos.usage_aggregates.list_for_workspace(workspace_id)
    rows = _filter_window(rows, from_, to)
    try:
        groups = aggregate_usage_rows(rows, group_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"totals": usage_totals_rows(rows), "groups": groups}


@router.get("/api/agents/{agent_id}/usage")
async def list_agent_usage(
    agent_id: UUID,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    group_by: Annotated[str, Query()] = "backend",
    repos: Repositories = Depends(get_repositories),
) -> dict:
    if await repos.agents.get(agent_id) is None:
        raise HTTPException(status_code=404, detail="agent not found")
    rows = await repos.usage_aggregates.list_for_agent(agent_id)
    rows = _filter_window(rows, from_, to)
    try:
        groups = aggregate_usage_rows(rows, group_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "agent_id": str(agent_id),
        "totals": usage_totals_rows(rows),
        "groups": groups,
    }
