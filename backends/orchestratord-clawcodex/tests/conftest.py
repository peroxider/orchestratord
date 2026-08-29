"""conftest for the orchestratord-clawcodex backend's own test suite.

Lives inside ``backends/orchestratord-clawcodex/tests/`` so tests that
stub the clawcodex SDK namespace (``extensions.api.query``) can live
alongside the backend code that wraps it.  See COUPLING_audit.md §8
(long-term plan) and §9 #5 acceptance criterion.

Path setup: backend source first (so ``import orchestratord_clawcodex``
resolves locally), then the monorepo source (so ``from orchestratord
import …`` resolves to the in-tree core).  Paths are computed relative
to this file, not the invocation cwd, so pytest works from any
working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND_SRC = (_HERE.parents[1] / "src").resolve()   # backends/.../src
_REPO_SRC = (_HERE.parents[2] / "src").resolve()      # <repo_root>/src

for _p in (str(_BACKEND_SRC), str(_REPO_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
