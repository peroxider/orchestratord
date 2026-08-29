#!/usr/bin/env python3
"""Probe: list all Read snapshots for a file across transcripts + tool-results dumps."""
import glob, json, re, sys

READ_LINE_RE = re.compile(r"^\s*(\d+)\t?(.*)$")
TOO_LARGE_RE = re.compile(r"Output too large \([\d.]+KB\)\. Full output saved to: (\S+)")
DIRS = [
    "/home/chad/.claude/projects/-mnt-c-WorkSpace-orchestratord",
    "/home/chad/.claude/projects/-mnt-c-WorkSpace",
]

target = sys.argv[1]
pending = {}
snaps = []

for root in DIRS:
    files = glob.glob(root + "/*.jsonl")
    tr_files = glob.glob(root + "/**/tool-results/*.txt", recursive=True)
    tr_map = {}
    for t in tr_files:
        try:
            tr_map[t] = open(t, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    for fp in sorted(files):
        for line in open(fp, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            ts = d.get("timestamp") or "?"
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and (b.get("name") or "") == "Read":
                    p = (b.get("input") or {}).get("file_path") or ""
                    if target in p:
                        pending[b.get("id")] = (ts, p)
                elif b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid in pending:
                        ts0, p = pending.pop(tid)
                        c = b.get("content")
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list):
                            text = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
                        else:
                            text = None
                        if text is None:
                            continue
                        m = TOO_LARGE_RE.search(text)
                        if m and m.group(1) in tr_map:
                            text = tr_map[m.group(1)]
                        numbered = sum(1 for ln in text.split("\n") if READ_LINE_RE.match(ln))
                        total = len(text.split("\n"))
                        snaps.append((ts0, p, len(text), total, numbered, text[:60].replace("\n", "\\n")))

snaps.sort()
for s in snaps:
    print(f"{s[0]}  {s[2]:7d}B {s[3]:5d} lines ({s[4]:5d} numbered)  {s[1]}")
    print(f"    head: {s[5]}")
print(f"total snapshots: {len(snaps)}")
