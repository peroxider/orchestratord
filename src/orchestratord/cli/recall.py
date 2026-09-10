"""``orchestratord recall`` — search learnings / rules / local issues.

Read-only retrieval over the experience loop's recall index
(DESIGN_EXPERIENCE_LOOP §8). Output is plain text grouped by doc kind;
rule hits are annotated "约定" and must never be presented as editable.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from orchestratord.recall import build_index, search

_KIND_CHOICES = ("learning", "rule", "issue")

_KIND_LABELS = {
    "learning": "Learnings",
    "rule": "Rules（约定）",
    "issue": "Local issues",
}


def add_recall_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "recall",
        help="Search learnings, rules and local issue docs (experience recall)",
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="Query text (ASCII words or Chinese; CJK uses bigram matching)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repository hint — same-repo docs get a ranking boost",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Maximum number of results (default: 10)",
    )
    parser.add_argument(
        "--type",
        dest="kind",
        choices=_KIND_CHOICES,
        default=None,
        help="Restrict results to one document kind",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root whose .orchestratord/learnings are indexed",
    )


def _render(hits) -> str:
    lines: list[str] = []
    current_kind: str | None = None
    for hit in hits:
        doc = hit.doc
        if doc.kind != current_kind:
            current_kind = doc.kind
            lines.append(f"## {_KIND_LABELS[current_kind]}")
        where = doc.source_path or doc.doc_id
        tag_part = f" [{', '.join(doc.tags)}]" if doc.tags else ""
        repo_part = f" repo={doc.repo}" if doc.repo else ""
        lines.append(f"- {doc.title}{tag_part}{repo_part}  (score={hit.score:.4f})")
        lines.append(f"  {where}")
        snippet = " ".join(doc.body.split())[:160]
        if snippet:
            lines.append(f"  {snippet}{'…' if len(snippet) == 160 else ''}")
    return "\n".join(lines) if lines else "No matching documents."


def run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    docs = build_index(workspace_root=workspace)
    hits = search(
        docs,
        " ".join(args.query),
        top_k=args.top_k,
        repo=args.repo,
        kinds=[args.kind] if args.kind else None,
    )
    print(_render(hits))
    return 0
