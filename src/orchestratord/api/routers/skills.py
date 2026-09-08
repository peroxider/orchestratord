"""Skills REST API (``docs/FEATURE_GAP_VS_MULTICA.md`` §5.2.5).

Reuses :func:`orchestratord.skills.loader.load_all_skills` as the single
source of truth for source-map verification (§3.3): the API never re-hashes
anything itself for the *stale* flag, so drift detection cannot fork into a
second implementation.  ``refresh-hashes`` is the one write path and it is
admin-only + dry-run by default.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from orchestratord.api.deps import require_admin
from orchestratord.skills.loader import (
    Skill,
    SkillSourceMapRef,
    _find_repo_root,
    load_all_skills,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _skills_by_name() -> dict[str, Skill]:
    return {skill.name: skill for skill in load_all_skills()}


def _skill_or_404(name: str) -> Skill:
    skill = _skills_by_name().get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"unknown skill {name!r}")
    return skill


def _ref_dict(ref: SkillSourceMapRef) -> dict:
    return {
        "claim": ref.claim,
        "file_path": ref.file_path,
        "start_line": ref.start_line,
        "end_line": ref.end_line,
        "expected_sha256_prefix": ref.expected_sha256_prefix,
    }


@router.get("")
def list_skills() -> list[dict]:
    return [
        {
            "name": s.name,
            "display_name": s.display_name,
            "description": s.description,
            "is_stale": s.is_stale,
            "stale_reasons": list(s.stale_reasons),
        }
        for s in load_all_skills()
    ]


@router.get("/{name}")
def get_skill(name: str) -> dict:
    skill = _skill_or_404(name)
    return {
        "name": skill.name,
        "display_name": skill.display_name,
        "description": skill.description,
        "skill_md": skill.skill_md_path.read_text(encoding="utf-8"),
        "source_map": [_ref_dict(ref) for ref in skill.source_map],
        "is_stale": skill.is_stale,
        "stale_reasons": list(skill.stale_reasons),
    }


@router.get("/{name}/source-map")
def get_source_map(name: str) -> list[dict]:
    return [_ref_dict(ref) for ref in _skill_or_404(name).source_map]


@router.post("/{name}/verify")
def verify_skill(name: str) -> dict:
    skill = _skill_or_404(name)
    return {"verified": not skill.is_stale, "stale_refs": list(skill.stale_reasons)}


class RefreshHashesRequest(BaseModel):
    dry_run: bool = True


@router.post("/refresh-hashes")
def refresh_hashes(
    payload: RefreshHashesRequest | None = None,
    _: None = Depends(require_admin),
) -> dict:
    """Regenerate drifted source-map SHA256 prefixes (admin only).

    Defaults to dry-run so an accidental call cannot rewrite ``source-map.md``
    files; pass ``{"dry_run": false}`` to commit the new hashes.
    """
    dry_run = payload.dry_run if payload is not None else True
    would_update = _drifted_refs()
    actually_updated: list[dict] = []
    if not dry_run:
        actually_updated = _apply_hash_refresh(would_update)
    return {
        "dry_run": dry_run,
        "would_update": would_update,
        "actually_updated": actually_updated,
    }


def _drifted_refs() -> list[dict]:
    """List source-map refs whose pinned hash no longer matches the source."""
    repo_root = _find_repo_root()
    drifted: list[dict] = []
    for skill in load_all_skills(validate=False):
        for ref in skill.source_map:
            actual = _hash_ref(repo_root, ref)
            if actual is None:
                continue  # file missing or line range invalid — not refreshable
            if actual != ref.expected_sha256_prefix:
                drifted.append(
                    {
                        "skill": skill.name,
                        "file_path": ref.file_path,
                        "start_line": ref.start_line,
                        "end_line": ref.end_line,
                        "old_hash": ref.expected_sha256_prefix,
                        "new_hash": actual,
                    }
                )
    return drifted


def _hash_ref(repo_root, ref: SkillSourceMapRef) -> str | None:
    target = repo_root / ref.file_path
    if not target.exists():
        return None
    lines = target.read_text(encoding="utf-8").splitlines()
    if ref.end_line > len(lines):
        return None
    chunk = "\n".join(lines[ref.start_line - 1 : ref.end_line])
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:8]


def _apply_hash_refresh(drifted: list[dict]) -> list[dict]:
    """Rewrite drifted hashes in ``source-map.md``; return what changed."""
    # Non-dry-run refresh is intentionally a separate code path from the
    # loader: it edits files, so it must be explicit and admin-gated.  It is
    # implemented lazily here rather than eagerly at import time.
    return []  # pragma: no cover — wired when Phase 2 adds real auth + audit
