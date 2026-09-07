"""conftest for the orchestratord-reasonix backend's own test suite.

Path setup: backend source first (so ``import orchestratord_reasonix``
resolves locally), then the monorepo source (so ``from orchestratord
import …`` resolves to the in-tree core). Paths are computed relative to
this file, not the invocation cwd, so pytest works from any directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND_SRC = _HERE.parents[1] / "src"   # backends/orchestratord-reasonix/src
_REPO_SRC = _HERE.parents[3] / "src"      # <repo_root>/src

for _p in (str(_BACKEND_SRC), str(_REPO_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
