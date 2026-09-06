"""FastAPI application layer for orchestratord (Phase 0/1).

Bridges the existing daemon / workflow / SPI / skills modules to an HTTP
surface without reimplementing them.  See ``docs/FEATURE_GAP_VS_MULTICA.md``
§5 (Web frontend) and §6 (backend / data layer).
"""

from __future__ import annotations

from orchestratord.api.app import app

__all__ = ["app"]
