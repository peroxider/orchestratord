"""VCS integration models (§6.5): GitHub App installations + pull requests.

``installations`` records one GitHub App installation per workspace (the OAuth /
App-install handshake's persistent side). ``pull_requests`` is the rollup the
issue-detail PR view reads; it is written by the issue→PR application
(``orchestratord.applications.issue_pr``) and kept current by the GitHub
webhook handler (``orchestratord.api.routers.vcs``).

Per §6.1 no model declares a ``ForeignKey`` and no lookup index is created
inline — those are emitted as separate ``CREATE INDEX CONCURRENTLY``
migrations. The uniqueness invariants (one installation per
``installation_id``; one PR per ``(repo, number)``) are declared as inline
``UniqueConstraint``s so the dev/test ``create_all`` schema and the atomic
webhook upsert (``ON CONFLICT``) can both enforce them; production applies the
same-named UNIQUE indexes via migrations 0038/0040.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from orchestratord.db.base import Base


class GitHubInstallation(Base):
    __tablename__ = "installations"
    __table_args__ = (
        UniqueConstraint("installation_id", name="uq_installations_installation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[uuid.UUID]
    installation_id: Mapped[int]
    account_login: Mapped[str]
    created_at: Mapped[datetime]


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repo", "number", name="uq_pull_requests_repo_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    issue_id: Mapped[uuid.UUID | None]
    repo: Mapped[str]
    number: Mapped[int]
    title: Mapped[str]
    state: Mapped[str]
    head_sha: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
