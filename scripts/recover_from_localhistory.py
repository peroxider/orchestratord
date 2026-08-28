#!/usr/bin/env python3
"""Extract file contents from PyCharm LocalHistory storage by brute-force zlib scan.

The LocalHistory format is proprietary; instead of fully parsing the record index,
we scan storageData for zlib deflate streams, decompress every candidate, and keep
blobs that match per-file marker regexes. For each target file, the largest blob
(with the most matching markers) wins.

Usage:
  python3 scripts/recover_from_localhistory.py \
      --storage "C:/Users/chad/AppData/Local/JetBrains/PyCharmCE2024.3/LocalHistory" \
      --out /mnt/c/WorkSpace/orchestratord --dry-run|write
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zlib

# Markers per target file (relative to repo root). A blob must match ALL of
# required markers; score = number matched. Tune as needed.
TARGETS: dict[str, dict] = {
    "src/orchestratord/agent_runner.py": {
        "required": ["class AgentRunner", "async def run", "def _append_skill_index"],
        "any": ["permission_mode", "max_turns", "system_prompt_append"],
    },
    "src/orchestratord/backend_registry.py": {
        "required": ["def resolve", "entry_points", "orchestratord.backends"],
        "any": ["importlib.metadata", "backend_registry"],
    },
    "pyproject.toml": {
        "required": ["[project]", "orchestratord"],
        "any": ["[project.scripts]", "[project.entry-points"],
    },
}


def zlib_streams(data: bytes):
    """Yield decompressed blobs from every plausible zlib stream offset."""
    n = len(data)
    i = 0
    while True:
        i = data.find(b"\x78", i)
        if i < 0 or i >= n - 2:
            return
        cmf = data[i]
        flg = data[i + 1]
        if ((cmf << 8) + flg) % 31 == 0 and (cmf & 0x0F) == 8:
            try:
                d = zlib.decompressobj()
                blob = d.decompress(data[i:])
                blob += d.flush()
                if len(blob) > 64:
                    yield i, blob
                    i += len(d.unused_data) + 10
                    continue
            except zlib.error:
                pass
        i += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storage", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["dry-run", "write"], default="dry-run")
    ap.add_argument("--data-file", default="changes.storageData")
    args = ap.parse_args()

    data_path = os.path.join(args.storage, args.data_file)
    data = open(data_path, "rb").read()
    print(f"storageData: {len(data)} bytes")

    blobs: list[tuple[int, bytes]] = []
    for off, blob in zlib_streams(data):
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # quick filter: only text with code/markdown flavor
        if "\x00" in text:
            continue
        blobs.append((off, blob))
    print(f"utf8 blobs: {len(blobs)}")

    for rel, spec in TARGETS.items():
        best = None  # (score, size, off, text)
        for off, blob in blobs:
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not all(m in text for m in spec["required"]):
                continue
            score = sum(1 for m in spec["any"] if m in text)
            cand = (score, len(blob), off, text)
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand
        if best is None:
            print(f"MISS  {rel}")
            continue
        score, size, off, text = best
        print(f"HIT   {rel}  score={score} size={size} off={off}")
        if args.mode == "write":
            dest = os.path.join(args.out, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
