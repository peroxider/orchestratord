"""orchestratord-clawcodex — ClawCodex isolated SDK backend.

Wraps ``extensions.api.query.QueryRunner`` in the orchestratord SPI
so that the existing clawcodex agent runtime can be driven as a
standard orchestratord backend.
"""

import os
import sys

# Ensure the clawcodex-ascend source tree is on sys.path so the
# ``extensions.*`` packages are importable.  The backend already depends
# on the clawcodex runtime by contract; this just resolves the filesystem
# location.  Must happen at package import time (before any lazy imports
# in orchestratord.adapters.clawcodex fire).
_CCX_SOURCE = os.environ.get(
    "CLAWCODEX_SOURCE",
    "/mnt/c/WorkSpace/AgentSDK/clawcodex-ascend",
)
if _CCX_SOURCE not in sys.path:
    sys.path.insert(0, _CCX_SOURCE)

from orchestratord_clawcodex.backend import ClawcodexBackend

__all__ = ["ClawcodexBackend"]
