"""``orchestratord web`` — launch the Next.js Web dashboard (§10.1).

Companion to ``serve`` (FastAPI backend) and ``dashboard`` (the
deprecated zero-dependency LiveView, retained ≥ 1 release cycle per
§3.6). This command runs the ``apps/web`` Next.js client; production
mode expects a prior ``next build``, ``--dev`` runs the dev server.

The web app directory resolves as ``--web-dir`` → ``ORCHESTRATORD_WEB_DIR``
→ the repo layout (``<repo>/apps/web``); a checkout without
``package.json`` aborts with an actionable message instead of spawning
a doomed subprocess.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

DEFAULT_WEB_PORT = 3100


def _default_web_dir() -> Path:
    # src/orchestratord/cli/web.py → parents[3] is the repo root.
    return Path(__file__).resolve().parents[3] / "apps" / "web"


def resolve_web_dir(explicit: str | None = None) -> Path:
    """Locate the Next.js app directory or exit with a clear message."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_dir = os.environ.get("ORCHESTRATORD_WEB_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(_default_web_dir())
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate
    tried = ", ".join(str(c) for c in candidates)
    raise SystemExit(
        "orchestratord web: Next.js app directory not found "
        f"(looked in: {tried}). Point --web-dir or ORCHESTRATORD_WEB_DIR "
        "at your apps/web checkout."
    )


def _spawn(cmd: list[str], cwd: Path) -> subprocess.Popen:
    """Subprocess seam — monkeypatched in tests."""
    try:
        return subprocess.Popen(cmd, cwd=cwd)
    except FileNotFoundError:
        raise SystemExit(
            "orchestratord web: 'pnpm' not found on PATH — install pnpm "
            "(https://pnpm.io) to run the Next.js web client."
        ) from None


def web_command(
    web_dir: Path, *, port: int, hostname: str | None, dev: bool
) -> list[str]:
    """The argv used to launch Next.js from *web_dir*."""
    cmd = [
        "pnpm",
        "exec",
        "next",
        "dev" if dev else "start",
        "--port",
        str(port),
    ]
    if hostname:
        cmd += ["--hostname", hostname]
    return cmd


def launch_web_process(
    *,
    web_dir: str | None = None,
    port: int = DEFAULT_WEB_PORT,
    hostname: str | None = None,
    dev: bool = False,
) -> subprocess.Popen:
    """Spawn the Next.js server; used by ``serve --with-web`` too."""
    directory = resolve_web_dir(web_dir)
    return _spawn(
        web_command(directory, port=port, hostname=hostname, dev=dev), directory
    )


def add_web_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``web`` subcommand."""
    web_parser = subparsers.add_parser(
        "web",
        help="Start the Next.js Web dashboard (apps/web)",
        description=(
            "Run the Next.js Web client from apps/web (§10.1). "
            "Production mode expects a prior `next build`."
        ),
    )
    web_parser.add_argument(
        "--host",
        default=None,
        help="Bind hostname forwarded to Next.js",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"Bind port (default: {DEFAULT_WEB_PORT})",
    )
    web_parser.add_argument(
        "--dev",
        action="store_true",
        help="Run `next dev` instead of `next start`",
    )
    web_parser.add_argument(
        "--web-dir",
        default=None,
        help=(
            "Path to the apps/web checkout (default: env "
            "ORCHESTRATORD_WEB_DIR, then the repo layout)"
        ),
    )


def run(args: argparse.Namespace) -> int:
    """Run the Next.js server in the foreground."""
    directory = resolve_web_dir(args.web_dir)
    cmd = web_command(directory, port=args.port, hostname=args.host, dev=args.dev)
    print(f"orchestratord web: launching {' '.join(cmd)} (cwd={directory})")
    child = _spawn(cmd, directory)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        return 130
