#!/usr/bin/env python3
"""Recalculate SHA256 prefixes in skills' references/source-map.md.

Scans ``src/orchestratord/skills/builtin/*/references/source-map.md``,
recomputes the hash of every pinned line range, and rewrites rows whose
hash drifted.  Run this after editing source files referenced by a
skill — then review the diff to confirm the new source still says what
SKILL.md claims.

Usage::

    python scripts/regen_source_map.py            # rewrite drifted rows
    python scripts/regen_source_map.py --check    # exit 1 if drifted, no write
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_BUILTIN = REPO_ROOT / "src" / "orchestratord" / "skills" / "builtin"

_ROW_RE = re.compile(
    r"^(\|\s*.+?\s*\|\s*.+?\s*\|\s*)(\d+)-(\d+)(\s*\|\s*)([0-9a-f]{8})(\s*\|.*)$"
)


def _hash_range(path: Path, start: int, end: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    chunk = "\n".join(lines[start - 1 : end])
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:8]


def regen(*, check_only: bool) -> int:
    any_drift = False
    for map_path in sorted(SKILLS_BUILTIN.glob("*/references/source-map.md")):
        out_lines: list[str] = []
        changed = False
        for line in map_path.read_text(encoding="utf-8").splitlines():
            m = _ROW_RE.match(line)
            if m:
                prefix, start, end, mid, old_hash, suffix = m.groups()
                # resolve the referenced file from the row's own column
                file_col = line.split("|")[2].strip()
                target = REPO_ROOT / file_col
                if target.exists():
                    actual = _hash_range(target, int(start), int(end))
                    if actual != old_hash:
                        any_drift = True
                        changed = True
                        line = f"{prefix}{start}-{end}{mid}{actual}{suffix}"
            out_lines.append(line)
        if changed:
            print(f"{'DRIFT' if check_only else 'FIXED'}: {map_path.relative_to(REPO_ROOT)}")
            if not check_only:
                map_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    if check_only and any_drift:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Only report drift; exit 1 if any hash is stale",
    )
    args = ap.parse_args()
    return regen(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
