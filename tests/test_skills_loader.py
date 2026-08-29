"""Tests for orchestratord.skills.loader — discovery + source-map validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orchestratord.skills.loader import (
    Skill,
    SkillSourceMapRef,
    _find_repo_root,
    _load_one_skill,
    _load_source_map,
    _split_frontmatter,
    _validate_ref,
    load_all_skills,
)


class TestBuiltinDiscovery:
    def test_loads_all_builtin_skills(self):
        skills = load_all_skills()
        names = {s.name for s in skills}
        assert {"mode-selector", "capability-explainer", "failure-recovery"} <= names

    def test_every_skill_has_description_and_source_map(self):
        for s in load_all_skills():
            assert s.description, s.name
            assert s.source_map, s.name
            assert s.skill_md_path.exists()

    def test_builtin_source_maps_are_fresh(self):
        """Guard: the shipped skills must validate against current sources."""
        stale = [s.name for s in load_all_skills() if s.is_stale]
        assert stale == [], f"stale builtin skills: {stale}"

    def test_fields_parsed_from_frontmatter(self):
        skills = {s.name: s for s in load_all_skills()}
        mode = skills["mode-selector"]
        assert mode.display_name == "Mode Selector"
        assert mode.user_invocable is False
        assert mode.allowed_tools == ()
        assert mode.version == 1


class TestFrontmatterSplit:
    def test_splits_simple_document(self):
        fm, body = _split_frontmatter("---\nname: x\n---\n\n# Hello\n")
        assert "name: x" in fm
        assert "# Hello" in body

    def test_missing_delimiters_raises(self):
        with pytest.raises(ValueError):
            _split_frontmatter("no frontmatter here")


class TestSourceMapParsing:
    def test_parses_valid_rows(self, tmp_path):
        map_md = tmp_path / "source-map.md"
        map_md.write_text(
            "# Source Map\n\n"
            "| 声明 | 源文件 | 行号 | 期望 hash |\n"
            "|---|---|---|---|\n"
            "| claim a | src/x.py | 3-4 | deadbeef |\n",
            encoding="utf-8",
        )
        refs = _load_source_map(map_md)
        assert refs == [
            SkillSourceMapRef("claim a", "src/x.py", 3, 4, "deadbeef")
        ]

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_source_map(tmp_path / "nope.md") == []


class TestRefValidation:
    def _make_target(self, tmp_path: Path) -> SkillSourceMapRef:
        target = tmp_path / "code.py"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        chunk = "line2\nline3"
        return SkillSourceMapRef(
            claim="c",
            file_path=str(target.relative_to(tmp_path)),
            start_line=2,
            end_line=3,
            expected_sha256_prefix=hashlib.sha256(chunk.encode()).hexdigest()[:8],
        ), tmp_path

    def test_valid_ref_passes(self, tmp_path, monkeypatch):
        ref, root = self._make_target(tmp_path)
        ok, reason = _validate_ref(ref, repo_root=root)
        assert ok is True
        assert reason == ""

    def test_wrong_hash_fails(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")
        ref = SkillSourceMapRef("c", "code.py", 2, 3, "00000000")
        ok, reason = _validate_ref(ref, repo_root=tmp_path)
        assert ok is False
        assert "hash mismatch" in reason

    def test_missing_file_fails(self, tmp_path):
        ref = SkillSourceMapRef("c", "ghost.py", 1, 1, "deadbeef")
        ok, reason = _validate_ref(ref, repo_root=tmp_path)
        assert ok is False
        assert "file not found" in reason

    def test_range_beyond_eof_fails(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("only\n", encoding="utf-8")
        ref = SkillSourceMapRef("c", "code.py", 1, 99, "deadbeef")
        ok, reason = _validate_ref(ref, repo_root=tmp_path)
        assert ok is False
        assert "exceeds file length" in reason


class TestOneSkillLoading:
    def _write_skill(self, tmp_path: Path, hash_prefix: str) -> Path:
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "references").mkdir(parents=True)
        target = tmp_path / "code.py"
        target.write_text("value = 1\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            "display_name: Demo Skill\n"
            "description: demo\n"
            "user_invocable: false\n"
            "allowed_tools: []\n"
            "version: 1\n"
            "---\n"
            "# Demo\n",
            encoding="utf-8",
        )
        (skill_dir / "references" / "source-map.md").write_text(
            "| c | code.py | 1-1 | " + hash_prefix + " |\n",
            encoding="utf-8",
        )
        return skill_dir

    def test_fresh_skill(self, tmp_path):
        skill_dir = self._write_skill(tmp_path, hashlib.sha256(b"value = 1").hexdigest()[:8])
        skill = _load_one_skill(skill_dir, skill_dir / "SKILL.md", tmp_path, validate=True)
        assert skill.is_stale is False
        assert skill.stale_reasons == ()

    def test_stale_skill_reports_reasons(self, tmp_path):
        skill_dir = self._write_skill(tmp_path, "00000000")
        skill = _load_one_skill(skill_dir, skill_dir / "SKILL.md", tmp_path, validate=True)
        assert skill.is_stale is True
        assert skill.stale_reasons

    def test_validate_false_skips_hashing(self, tmp_path):
        skill_dir = self._write_skill(tmp_path, "00000000")
        skill = _load_one_skill(skill_dir, skill_dir / "SKILL.md", tmp_path, validate=False)
        assert skill.is_stale is False


class TestRepoRoot:
    def test_finds_pyproject_root(self):
        root = _find_repo_root()
        assert (root / "pyproject.toml").exists()
