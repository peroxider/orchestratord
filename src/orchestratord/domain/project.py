"""Project entity model (§7.2).

A project groups a repository and documentation into a work set an agent
attaches as session context (multica "attach the repos and docs agents need
as context"). ``Project`` is the container; ``ProjectRepo`` / ``ProjectDoc``
are its members, referencing the project by ``project_id`` (referential
integrity is enforced at the repository layer, not here).

* ``ProjectDoc.doc_type`` is constrained to {md, html, pdf}.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

_DOC_TYPES = frozenset({"md", "html", "pdf"})


@dataclass
class Project:
    id: UUID
    workspace_id: UUID
    name: str
    description: str = ""


@dataclass
class ProjectRepo:
    project_id: UUID
    repo_url: str
    default_branch: str = "main"


@dataclass
class ProjectDoc:
    project_id: UUID
    doc_url: str
    doc_type: str

    def __post_init__(self) -> None:
        if self.doc_type not in _DOC_TYPES:
            raise ValueError(
                f"invalid doc_type {self.doc_type!r}; expected one of "
                f"{sorted(_DOC_TYPES)}"
            )
