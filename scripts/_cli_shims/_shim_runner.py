"""挡板脚本: pytest 期间任何调用此 CLI 即失败。

stdout: 一行说明,告知测试如何豁免
stderr: 详细帮助
exit code: 126 (multica sentinel)

Per DESIGN_backend_cli_test_guard.md §2.2. Shared by every shim binary
installed under the guard directory; the per-binary wrapper exports
``ORCHESTRATORD_GUARDED_BINARY`` / ``ORCHESTRATORD_GUARDED_BACKEND_PKG``
into the environment so this single module can attribute the failure
to the right backend package.
"""

import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    binary = os.environ.get("ORCHESTRATORD_GUARDED_BINARY", "<unknown>")
    pkg = os.environ.get("ORCHESTRATORD_GUARDED_BACKEND_PKG", "<unknown>")
    msg = (
        f"[backend-cli-guard] {binary!r} was invoked during pytest.\n"
        f"  argv: {argv!r}\n"
        f"  backend package: {pkg}\n"
        f"\n"
        f"  This is the default — pytest tests must NOT shell out to real agent CLIs.\n"
        f"  Use a SPI stub (tests/spi_stub_helpers.py) or mark the test with\n"
        f"  @pytest.mark.uses_real_cli and ensure the test is in tests/manual_e2e_*.py.\n"
    )
    print(msg, file=sys.stderr)
    return 126


if __name__ == "__main__":
    sys.exit(main())