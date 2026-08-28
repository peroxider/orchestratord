"""为 pytest 临时目录生成 5 个挡板可执行文件并返回路径。

conftest 调用一次,结果缓存到 os.environ["ORCHESTRATORD_CLI_GUARD_DIR"]。
每个 shim 都是一个 thin wrapper — 它设置两个环境变量然后
``from _shim_runner import main`` 让 5 个 binary 共享同一份源码。

Per DESIGN_backend_cli_test_guard.md §2.3.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import textwrap
from pathlib import Path

from orchestratord._backend_cli_registry import KNOWN_BACKEND_CLIS


def _render_shim_source(binary: str, backend_pkg: str, shim_runner_dir: str) -> str:
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import os, sys
        os.environ.setdefault("ORCHESTRATORD_GUARDED_BINARY", {binary!r})
        os.environ.setdefault("ORCHESTRATORD_GUARDED_BACKEND_PKG", {backend_pkg!r})
        sys.path.insert(0, {shim_runner_dir!r})
        from _shim_runner import main
        sys.exit(main())
    """)


def install_cli_shims(*, parent: Path | None = None) -> Path:
    """Generate the five stub executables into a directory and return it.

    Args:
        parent: Directory to populate. Defaults to a fresh
            ``tempfile.mkdtemp(prefix="orchestratord-cli-guard-")`` —
            callers should not rely on cleanup by the function itself;
            ``conftest`` only needs the path while the test runs, and
            the tempdir will be reaped by the OS.

    Returns:
        The directory containing ``_shim_runner.py`` plus one executable
        per backend CLI. Prepending this directory to ``$PATH`` causes
        any ``subprocess`` invocation of those names to fail with
        exit code 126.
    """
    base = parent or Path(tempfile.mkdtemp(prefix="orchestratord-cli-guard-"))
    base.mkdir(parents=True, exist_ok=True)
    shim_runner_src = Path(__file__).resolve().parent / "_cli_shims" / "_shim_runner.py"
    (base / "_shim_runner.py").write_text(
        shim_runner_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for cli in KNOWN_BACKEND_CLIS:
        target = base / cli.binary
        target.write_text(
            _render_shim_source(cli.binary, cli.backend_package, str(base))
        )
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Windows does not resolve extensionless files through PATH. Keep the
        # POSIX shim and add a .cmd companion that invokes the same runner.
        (base / f"{cli.binary}.cmd").write_text(
            "@echo off\r\n"
            f"set \"ORCHESTRATORD_GUARDED_BINARY={cli.binary}\"\r\n"
            f"set \"ORCHESTRATORD_GUARDED_BACKEND_PKG={cli.backend_package}\"\r\n"
            "python \"%~dp0_shim_runner.py\" %*\r\n",
            encoding="utf-8",
        )
    return base


if __name__ == "__main__":
    d = install_cli_shims(
        parent=Path(sys.argv[1]) if len(sys.argv) > 1 else None
    )
    print(d)
