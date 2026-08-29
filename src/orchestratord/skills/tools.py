"""Agent-facing skill tools.

``load_skill`` is the tool the agent calls to read a skill's full
SKILL.md body; ``build_skill_index`` renders the one-line-per-skill
index that gets appended to the system prompt.

Module state is cached after first use; tests can call
:func:`reset_cached_skills` to force a reload.
"""

from __future__ import annotations

from orchestratord.skills.loader import _split_frontmatter, load_all_skills

__all__ = ["SkillNotFoundError", "build_skill_index", "load_skill", "reset_cached_skills"]

_SKILLS: list | None = None
_BY_NAME: dict[str, object] | None = None


class SkillNotFoundError(LookupError):
    """Raised when ``load_skill`` is called with an unknown name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"skill not found: {name!r}")
        self.name = name


def _ensure_loaded() -> list:
    global _SKILLS, _BY_NAME
    if _SKILLS is None:
        # validate=False: source-map validation runs at daemon startup /
        # CI; the runtime path must not fail because a hash drifted.
        _SKILLS = load_all_skills(validate=False)
        _BY_NAME = {s.name: s for s in _SKILLS}
    return _SKILLS


def reset_cached_skills() -> None:
    """Drop the cached skill list (used by tests and hot reload)."""
    global _SKILLS, _BY_NAME
    _SKILLS = None
    _BY_NAME = None


def load_skill(name: str) -> str:
    """Read the full SKILL.md body for ``name``.

    Args:
        name: skill name (kebab-case), e.g. ``mode-selector``.

    Returns:
        The SKILL.md body without its frontmatter.

    Raises:
        SkillNotFoundError: no skill registered under ``name``.
    """
    _ensure_loaded()
    assert _BY_NAME is not None
    skill = _BY_NAME.get(name)
    if skill is None:
        raise SkillNotFoundError(name)
    text = skill.skill_md_path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    return body


def build_skill_index(*, base_append: str = "") -> str:
    """Render the skill index block appended to the system prompt.

    Returns ``base_append`` unchanged when no skills exist.
    """
    skills = _ensure_loaded()
    if not skills:
        return base_append
    index = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    parts = [p for p in (base_append.strip(),) if p]
    parts.append("## 可用 Skills（agent 可调用）")
    parts.append(index)
    parts.append("调用方式：使用 `load_skill(name='<name>')` 工具获取完整 SKILL.md。")
    return "\n\n".join(parts)
