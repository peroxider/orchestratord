"""Skill discovery + source-map validation.

Loads every ``src/orchestratord/skills/builtin/*/SKILL.md``, parses its
YAML frontmatter, and verifies each ``references/source-map.md`` row:
the referenced file must exist and the pinned line range must hash to
the recorded SHA256 prefix.  Any mismatch marks the skill ``stale`` so
the daemon can warn and the CLI ``verify`` command can fail CI.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_SOURCE_MAP_ROW_RE = re.compile(
    r"\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)-(\d+)\s*\|\s*([0-9a-f]{8})\s*\|"
)


@dataclass(frozen=True)
class SkillSourceMapRef:
    """One pinned claim: source file + line range + expected hash prefix."""

    claim: str
    file_path: str  # relative to the repo root
    start_line: int
    end_line: int
    expected_sha256_prefix: str


@dataclass(frozen=True)
class Skill:
    """A loaded agent-callable skill."""

    name: str
    display_name: str
    description: str
    user_invocable: bool
    allowed_tools: tuple[str, ...]
    version: int
    skill_md_path: Path
    source_map: tuple[SkillSourceMapRef, ...]
    tools_module: object | None
    is_stale: bool = False
    stale_reasons: tuple[str, ...] = ()


_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"


def load_all_skills(*, repo_root: Path | None = None, validate: bool = True) -> list[Skill]:
    """Scan ``_SKILL_ROOT`` and return every skill found.

    ``validate=False`` skips source-map hashing (used by the runtime
    prompt injection path, where validation already happened at startup
    or in CI).
    """
    skills: list[Skill] = []
    if not _SKILL_ROOT.is_dir():
        return skills
    for skill_dir in sorted(_SKILL_ROOT.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        skills.append(_load_one_skill(skill_dir, skill_md, repo_root, validate))
    return skills


def _load_one_skill(
    skill_dir: Path,
    skill_md: Path,
    repo_root: Path | None,
    validate: bool,
) -> Skill:
    text = skill_md.read_text(encoding="utf-8")
    fm, _body = _split_frontmatter(text)
    meta = yaml.safe_load(fm) or {}
    source_map = _load_source_map(skill_dir / "references" / "source-map.md")
    tools_module = _maybe_load_tools(skill_dir / "tools.py")

    stale = False
    reasons: list[str] = []
    if validate:
        for ref in source_map:
            ok, reason = _validate_ref(ref, repo_root)
            if not ok:
                stale = True
                reasons.append(reason)

    return Skill(
        name=str(meta["name"]),
        display_name=str(meta.get("display_name", meta["name"])),
        description=str(meta.get("description", "")),
        user_invocable=bool(meta.get("user_invocable", False)),
        allowed_tools=tuple(meta.get("allowed_tools", ()) or ()),
        version=int(meta.get("version", 1)),
        skill_md_path=skill_md,
        source_map=tuple(source_map),
        tools_module=tools_module,
        is_stale=stale,
        stale_reasons=tuple(reasons),
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split YAML frontmatter from the Markdown body."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("SKILL.md missing frontmatter delimiters '---'")
    return m.group(1), m.group(2)


def _load_source_map(path: Path) -> list[SkillSourceMapRef]:
    """Parse the source-map table into :class:`SkillSourceMapRef` rows."""
    if not path.exists():
        return []
    refs: list[SkillSourceMapRef] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _SOURCE_MAP_ROW_RE.match(line)
        if m:
            refs.append(
                SkillSourceMapRef(
                    claim=m.group(1),
                    file_path=m.group(2),
                    start_line=int(m.group(3)),
                    end_line=int(m.group(4)),
                    expected_sha256_prefix=m.group(5),
                )
            )
    return refs


def _validate_ref(ref: SkillSourceMapRef, repo_root: Path | None) -> tuple[bool, str]:
    """Verify one pinned reference: file exists + line-range hash matches."""
    root = repo_root or _find_repo_root()
    target = root / ref.file_path
    if not target.exists():
        return False, f"{ref.claim}: file not found: {ref.file_path}"
    lines = target.read_text(encoding="utf-8").splitlines()
    if ref.end_line > len(lines):
        return False, (
            f"{ref.claim}: line range {ref.start_line}-{ref.end_line} exceeds "
            f"file length {len(lines)}: {ref.file_path}"
        )
    chunk = "\n".join(lines[ref.start_line - 1 : ref.end_line])
    actual = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:8]
    if actual != ref.expected_sha256_prefix:
        return False, (
            f"{ref.claim}: hash mismatch at {ref.file_path}:"
            f"{ref.start_line}-{ref.end_line} expected={ref.expected_sha256_prefix} "
            f"actual={actual}"
        )
    return True, ""


def _maybe_load_tools(path: Path):
    """Import a skill's optional ``tools.py``; return None when absent."""
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"skill_tools_{path.parent.name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover — defensive
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_repo_root() -> Path:
    """Walk up from this file to the nearest ``pyproject.toml``."""
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "pyproject.toml").exists():
            return cur
        cur = cur.parent
    return Path.cwd()
