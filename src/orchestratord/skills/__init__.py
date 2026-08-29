"""Agent-callable skills — first-class knowledge packages.

A skill is a directory under ``skills/builtin/<name>/`` containing:

- ``SKILL.md`` — YAML frontmatter + Markdown body the agent reads
- ``references/source-map.md`` — every claim pinned to source lines
- ``tools.py`` (optional) — Python helpers the agent may call

The loader validates source-maps at import/startup time so documentation
rot is caught in CI instead of in production.
"""

from orchestratord.skills.loader import (
    Skill,
    SkillSourceMapRef,
    load_all_skills,
)

__all__ = ["Skill", "SkillSourceMapRef", "load_all_skills"]
