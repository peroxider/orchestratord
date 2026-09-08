"""Projects REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §7.2).

A project groups repos and docs into a work set an agent attaches as session
context. Phase 1 serves workspace-scoped CRUD over the persistence layer
(``orchestratord.db``); ``doc_type`` is validated by the domain model, and every
read re-checks ``workspace_id`` for tenant isolation. The Phase-5
session-injection behavior (attach repo paths + doc summaries at session start)
is not exercised here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.db import get_repositories
from orchestratord.db import models as orm
from orchestratord.db.repository import Repositories
from orchestratord.domain.project import ProjectDoc

router = APIRouter(tags=["projects"])


async def _project_payload(repos: Repositories, project: orm.Project) -> dict:
    repos_list = await repos.project_repos.list_for_project(project.id)
    docs_list = await repos.project_docs.list_for_project(project.id)
    return {
        "id": str(project.id),
        "workspace_id": str(project.workspace_id),
        "name": project.name,
        "description": project.description,
        "repos": [
            {"repo_url": r.repo_url, "default_branch": r.default_branch}
            for r in repos_list
        ],
        "docs": [
            {"doc_url": d.doc_url, "doc_type": d.doc_type}
            for d in docs_list
        ],
    }


async def _project_or_404(
    repos: Repositories, workspace_id: UUID, project_id: UUID
) -> orm.Project:
    project = await repos.projects.get(project_id)
    if project is None or project.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="project not found")
    return project


class _ProjectCreate(BaseModel):
    name: str
    description: str = ""


class _RepoCreate(BaseModel):
    repo_url: str
    default_branch: str = "main"


class _DocCreate(BaseModel):
    doc_url: str
    doc_type: str


@router.get("/api/workspaces/{workspace_id}/projects")
async def list_projects(
    workspace_id: UUID, repos: Repositories = Depends(get_repositories)
) -> list[dict]:
    projects = await repos.projects.list_for_workspace(workspace_id)
    return [await _project_payload(repos, p) for p in projects]


@router.post("/api/workspaces/{workspace_id}/projects", status_code=201)
async def create_project(
    workspace_id: UUID,
    body: _ProjectCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    project = orm.Project(
        id=uuid4(),
        workspace_id=workspace_id,
        name=body.name.strip(),
        description=body.description,
    )
    await repos.projects.add(project)
    return await _project_payload(repos, project)


@router.get("/api/workspaces/{workspace_id}/projects/{project_id}")
async def get_project(
    workspace_id: UUID,
    project_id: UUID,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    project = await _project_or_404(repos, workspace_id, project_id)
    return await _project_payload(repos, project)


@router.post(
    "/api/workspaces/{workspace_id}/projects/{project_id}/repos", status_code=201
)
async def add_repo(
    workspace_id: UUID,
    project_id: UUID,
    body: _RepoCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _project_or_404(repos, workspace_id, project_id)
    repo = orm.ProjectRepo(
        id=uuid4(),
        project_id=project_id,
        repo_url=body.repo_url,
        default_branch=body.default_branch,
    )
    await repos.project_repos.add(repo)
    return {
        "project_id": str(project_id),
        "repo_url": repo.repo_url,
        "default_branch": repo.default_branch,
    }


@router.post(
    "/api/workspaces/{workspace_id}/projects/{project_id}/docs", status_code=201
)
async def add_doc(
    workspace_id: UUID,
    project_id: UUID,
    body: _DocCreate,
    repos: Repositories = Depends(get_repositories),
) -> dict:
    await _project_or_404(repos, workspace_id, project_id)
    try:
        validated = ProjectDoc(
            project_id=project_id, doc_url=body.doc_url, doc_type=body.doc_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    doc = orm.ProjectDoc(
        id=uuid4(),
        project_id=project_id,
        doc_url=validated.doc_url,
        doc_type=validated.doc_type,
    )
    await repos.project_docs.add(doc)
    return {
        "project_id": str(project_id),
        "doc_url": doc.doc_url,
        "doc_type": doc.doc_type,
    }
