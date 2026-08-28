#!/usr/bin/env python3
"""Replay Claude Code transcripts to rebuild deleted files.

Strategy:
- Scan every *.jsonl transcript under the given project dirs (':'-separated).
- Collect chronological events: Write/Edit/MultiEdit tool_use, Read tool_use+result.
- Per file:
    * Stitch read snapshots by line number (latest timestamp wins per line).
    * Replay Write/Edit/MultiEdit on top in chronological order.
    * Write → set content; Edit → old->new replace; MultiEdit → sequential edits.
- Emit final state for every path under the project root.

Usage (from WSL):
  python3 scripts/recover_from_transcripts.py \
      --transcripts /home/chad/.claude/projects/-mnt-c-WorkSpace-orchestratord \
      --out /mnt/c/WorkSpace/orchestratord [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

PROJECT_PREFIXES = [
    "/mnt/c/WorkSpace/orchestratord/",
    "C:\\WorkSpace\\orchestratord\\",
    "c:\\WorkSpace\\orchestratord\\",
    "C:/WorkSpace/orchestratord/",
    "c:/WorkSpace/orchestratord/",
]
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

READ_LINE_RE = re.compile(r"^\s*(\d+)\t?(.*)$")


def normalize_path(raw: str) -> str | None:
    for prefix in PROJECT_PREFIXES:
        if raw.startswith(prefix):
            rest = raw[len(prefix):].replace("\\", "/")
            parts = [p for p in rest.split("/") if p and p not in SKIP_DIRS]
            return "/".join(parts) if parts else None
    return None


def iter_events(transcript_dir: str):
    events: list[tuple[str, int, str, object]] = []
    seq = 0
    all_files: list[str] = []
    for root in transcript_dir.split(os.pathsep):
        all_files.extend(glob.glob(f"{root}/*.jsonl"))
    for fp in sorted(set(all_files)):
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                ts = d.get("timestamp") or ""
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name") or ""
                        if name in ("Write", "Edit", "MultiEdit", "Read"):
                            events.append((ts, seq, "tool_use",
                                           {"name": name, "input": block.get("input") or {}, "id": block.get("id")}))
                            seq += 1
                    elif btype == "tool_result":
                        events.append((ts, seq, "tool_result",
                                       {"tool_use_id": block.get("tool_use_id"), "block": block}))
                        seq += 1
    events.sort(key=lambda e: (e[0], e[1]))
    for ts, _, kind, payload in events:
        yield ts, kind, payload


def read_result_text(block: dict) -> str | None:
    content = block.get("content")
    if content is None:
        return None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif isinstance(c, str):
                texts.append(c)
        text = "\n".join(texts)
    else:
        return None
    return text


TOO_LARGE_RE = re.compile(r"Output too large \([\d.]+KB\)\. Full output saved to: (\S+)")


def resolve_tool_result_file(text: str) -> str | None:
    m = TOO_LARGE_RE.search(text)
    return m.group(1) if m else None


def parse_numbered_lines(text: str):
    """Strictly parse Read output with 'N\\tline' prefixes; unnumbered lines are dropped."""
    out: list[str | None] = []
    start = None
    expect = 0
    for ln in text.split("\n"):
        m = READ_LINE_RE.match(ln)
        if not m:
            continue  # drop truncation notices / stray lines
        n = int(m.group(1))
        s = m.group(2)
        if start is None:
            start = n
            expect = n
        while n > expect:
            out.append("")
            expect += 1
        if n == expect:
            out.append(s)
            expect += 1
        elif n < expect and out:
            out[n - start] = s
    if start is None or not out:
        return None
    return start, out


class PathState:
    def __init__(self):
        self.reads: list[tuple[str, int, list[str | None]]] = []  # (ts, start, lines)
        self.ops: list[tuple[str, dict, str]] = []  # (ts, input, kind)
        self.last_op_ts = ""
        self.last_op = ""


def stitch(state: PathState) -> str:
    if not state.reads:
        return ""
    latest: dict[int, tuple[str, str]] = {}
    for ts, start, lines in state.reads:
        for i, s in enumerate(lines):
            n = start + i
            if s is None:
                continue
            if n not in latest or ts >= latest[n][0]:
                latest[n] = (ts, s)
    if not latest:
        return ""
    maxline = max(latest.keys())
    out = []
    for n in range(1, maxline + 1):
        out.append(latest.get(n, (None, ""))[1])
    return "\n".join(out)


def apply_edit(content: str, old: str, new: str, replace_all: bool) -> str | None:
    if old not in content:
        return None
    return content.replace(old, new) if replace_all else content.replace(old, new, 1)


def replay(base: str, ops, start_ts: str | None = None):
    content = base
    applied = failed = 0
    for ts, inp, name in ops:
        if start_ts is not None and ts <= start_ts:
            continue
        if name == "Write":
            content = inp.get("content") or ""
            applied += 1
        elif name == "Edit":
            old = inp.get("old_string") or ""
            new = inp.get("new_string") or ""
            ra = bool(inp.get("replace_all"))
            nc = apply_edit(content, old, new, ra)
            if nc is None:
                failed += 1
            else:
                content = nc
                applied += 1
        elif name == "MultiEdit":
            bad = False
            for e in inp.get("edits") or []:
                nc = apply_edit(content, e.get("old_string") or "", e.get("new_string") or "",
                                bool(e.get("replace_all")))
                if nc is None:
                    bad = True
                else:
                    content = nc
            if bad:
                failed += 1
            else:
                applied += 1
    return content, applied, failed


def candidate_ok(rel: str, content: str) -> bool:
    if not content.strip():
        return False
    try:
        if rel.endswith(".py"):
            compile(content, rel, "exec")
            return True
        if rel.endswith(".toml"):
            import tomllib
            tomllib.loads(content)
            return True
        if rel.endswith(".json"):
            json.loads(content)
            return True
    except (SyntaxError, ValueError, json.JSONDecodeError):
        return False
    # plain text: reject obvious garbage heads
    head = content[:120]
    return not re.search(r"File does not exist|Output too large", head)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    states: dict[str, PathState] = defaultdict(PathState)
    read_inputs: dict[str, tuple[str, str]] = {}  # tool_use_id -> (ts, path)
    stats: dict[str, int] = defaultdict(int)
    unapplied: list[tuple[str, str]] = []

    for ts, kind, payload in iter_events(args.transcripts):
        if kind == "tool_result":
            tu_id = payload["tool_use_id"]
            if tu_id in read_inputs:
                _ts, path = read_inputs.pop(tu_id)
                text = read_result_text(payload["block"])
                if text is None:
                    continue
                saved = resolve_tool_result_file(text)
                if saved:
                    try:
                        with open(saved, encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        continue
                parsed = parse_numbered_lines(text)
                if parsed is None:
                    continue
                start, lines = parsed
                states[path].reads.append((_ts, start, lines))
                stats["reads"] += 1
            continue

        name = payload["name"]
        inp = payload["input"]
        raw_path = inp.get("file_path") or inp.get("notebook_path") or ""
        path = normalize_path(raw_path)
        if path is None:
            continue
        if name == "Read":
            tu_id = payload.get("id")
            if tu_id:
                read_inputs[tu_id] = (ts, path)
            continue
        stats["ops"] += 1
        st = states[path]
        st.ops.append((ts, inp, name))
        st.last_op_ts = ts
        st.last_op = name.lower()

    files = sorted(states.keys())
    print(f"paths touched: {len(files)}")
    print(f"stats: {dict(stats)}")

    os.makedirs(args.out, exist_ok=True)
    written = 0
    for rel in files:
        st = states[rel]
        # Candidate reconstructions:
        #  A) empty base + all ops (works when chain starts with Write)
        #  B) stitched base + all ops
        #  C) each single read snapshot + only ops after it
        # Pick the candidate with most applied ops, then fewest failures,
        # then the largest content (avoid truncation artifacts).
        candidates = []
        candidates.append(replay("", st.ops))
        stitched = stitch(st)
        if stitched:
            candidates.append(replay(stitched, st.ops))
        for r_ts, r_start, r_lines in st.reads:
            snap = "\n".join(s or "" for s in r_lines)
            candidates.append(replay(snap, st.ops, start_ts=r_ts))
        # Prefer candidates that pass syntax validation, then most applied
        # ops, fewest failures, largest content.
        scored = []
        for c in candidates:
            ok = candidate_ok(rel, c[0])
            scored.append((1 if ok else 0, c[1], -c[2], len(c[0]), c[0]))
        _, applied, negfail, _, content = max(scored, key=lambda s: s[:4])
        failed = -negfail
        if failed == 0 and not applied and not candidates:
            continue
        src = f"{applied} ops, {failed} failed"
        if failed:
            unapplied.append((rel, f"{failed} failed"))
        dest = os.path.join(args.out, *rel.split("/"))
        if not os.path.splitext(dest)[1]:
            continue
        if args.dry_run:
            print(f"  WOULD WRITE {rel} ({len(content)} bytes, {src})")
            written += 1
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except FileExistsError:
            stats["skipped_dir_collision"] += 1
            continue
        if os.path.isdir(dest):
            continue
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        written += 1

    print(f"unapplied: {len(unapplied)}")
    for p, r in unapplied[:30]:
        print(f"  UNAPPLIED {r}: {p}")
    print(f"written: {written} files -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
