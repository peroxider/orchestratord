"""Project entity invariants (§7.2).

Projects group a repo + docs into a work set an agent attaches as session
context. Invariants:

* ``project_repos`` carry ``(project_id, repo_url, default_branch)``.
* ``project_docs`` carry ``(project_id, doc_url, doc_type)`` with
  ``doc_type`` ∈ {md, html, pdf}.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.2.
"""
from __future__ import annotations

from uuid import uuid4

import pytest


def _project(**overrides):
    from orchestratord.domain.project import Project  # type: ignore

    defaults = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "name": "docs-site",
        "description": "team docs",
    }
    defaults.update(overrides)
    return Project(**defaults)


class TestProjectFields:
    """All required fields present; description has a safe default."""

    def test_required_fields_present(self) -> None:
        p = _project()
        for key in ("id", "workspace_id", "name", "description"):
            assert getattr(p, key) is not None, f"missing {key!r}"

    def test_description_defaults_to_empty(self) -> None:
        from orchestratord.domain.project import Project  # type: ignore

        p = Project(id=uuid4(), workspace_id=uuid4(), name="x")
        assert p.description == ""


class TestProjectDocType:
    """``doc_type`` is constrained to {md, html, pdf}."""

    def test_known_doc_types_accepted(self) -> None:
        from orchestratord.domain.project import ProjectDoc  # type: ignore

        for kind in ("md", "html", "pdf"):
            d = ProjectDoc(project_id=uuid4(), doc_url="https://x", doc_type=kind)
            assert d.doc_type == kind

    def test_unknown_doc_type_rejected(self) -> None:
        from orchestratord.domain.project import ProjectDoc  # type: ignore

        with pytest.raises(ValueError, match="doc_type"):
            ProjectDoc(project_id=uuid4(), doc_url="https://x", doc_type="docx")


class TestProjectRepo:
    """Repos reference their project and carry a default branch."""

    def test_repo_fields_round_trip(self) -> None:
        from orchestratord.domain.project import ProjectRepo  # type: ignore

        pid = uuid4()
        r = ProjectRepo(
            project_id=pid,
            repo_url="https://github.com/acme/docs",
            default_branch="main",
        )
        assert r.project_id == pid
        assert r.repo_url == "https://github.com/acme/docs"
        assert r.default_branch == "main"

    def test_default_branch_defaults_to_main(self) -> None:
        from orchestratord.domain.project import ProjectRepo  # type: ignore

        r = ProjectRepo(project_id=uuid4(), repo_url="https://github.com/a/b")
        assert r.default_branch == "main"
