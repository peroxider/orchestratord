"""Token cost estimator (§7.2): tokens + model → USD.

Backends whose ``capabilities`` advertise ``cost_reporting=False`` (or that
simply don't return a cost) still land on the usage page: the usage
ingestion path calls :func:`estimate_cost_usd` when a record arrives with
``cost_usd == 0`` and a resolvable model. Prices come from
``packages/core/src/pricing/pricing.json`` (the canonical table, shared
with the Web client) — overridable via ``ORCHESTRATORD_PRICING_JSON``.
Model ids frequently carry date suffixes (``claude-sonnet-4-5-20250929``),
so matching is exact id → longest prefixed id → table ``default``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Last-resort table so an estimator consumer never crashes when neither
# the repo layout nor an env override is present (installed wheels).
_BUILTIN_PRICING: dict[str, Any] = {
    "default": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    "models": {},
}

# Repo-relative location of the canonical table (§7.2 spec path).
_REPO_PRICING = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "core"
    / "src"
    / "pricing"
    / "pricing.json"
)

_pricing_cache: dict[str, Any] | None = None


def load_pricing() -> dict[str, Any]:
    """Load the pricing table (env override → repo file → built-in)."""
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    candidates: list[Path] = []
    env_path = os.environ.get("ORCHESTRATORD_PRICING_JSON")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(_REPO_PRICING)
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                table = json.load(fh)
            if isinstance(table, dict):
                _pricing_cache = table
                return table
        except (OSError, ValueError):
            logger.debug("pricing table %s unreadable", path, exc_info=True)
    return _BUILTIN_PRICING


def reset_pricing_cache() -> None:
    """Drop the cached table (tests / config reloads)."""
    global _pricing_cache
    _pricing_cache = None


def resolve_model_alias(model: str, aliases: dict[str, str]) -> str:
    """Map a requested model id onto the actually-served one.

    ``aliases`` maps a requested-model pattern onto the real model id,
    matched exactly first, then by longest prefix (model ids often carry
    date suffixes). Unmatched ids pass through unchanged — the alias is
    a best-effort correction for gateways that serve model X under a
    different label (e.g. ccb reporting haiku while serving glm).
    """
    if not aliases or not model:
        return model
    if model in aliases:
        return aliases[model]
    best: tuple[int, str] | None = None
    for known, actual in aliases.items():
        if model.startswith(known) and (best is None or len(known) > best[0]):
            best = (len(known), actual)
    return best[1] if best is not None else model


def _rate_for(
    model: str, pricing: dict[str, Any], *, allow_default: bool = True
) -> dict[str, float] | None:
    models = pricing.get("models") or {}
    if model in models:
        return models[model]
    best: tuple[int, dict[str, float]] | None = None
    for known, rates in models.items():
        if model.startswith(known) and (best is None or len(known) > best[0]):
            best = (len(known), rates)
    if best is not None:
        return best[1]
    if not allow_default:
        return None
    default = pricing.get("default")
    return default if isinstance(default, dict) else None


def estimate_cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    pricing: dict[str, Any] | None = None,
    *,
    allow_default: bool = True,
) -> float | None:
    """Estimated USD for one usage record, or ``None`` if unpriceable.

    ``model`` may be empty — the table ``default`` still applies when
    present; ``None`` comes back only when no rate of any kind resolves.
    With ``allow_default=False`` an unknown model yields ``None`` instead
    of silently costing at the ``default`` rates (used when re-estimating
    cost for an aliased model — a wrong default is worse than keeping the
    reported figure).
    """
    table = pricing if pricing is not None else load_pricing()
    rates = _rate_for(model or "", table, allow_default=allow_default)
    if rates is None:
        return None
    cost = (
        tokens_in * float(rates.get("input_per_mtok", 0.0))
        + tokens_out * float(rates.get("output_per_mtok", 0.0))
    ) / 1_000_000.0
    return cost


__all__ = ["estimate_cost_usd", "load_pricing", "reset_pricing_cache", "resolve_model_alias"]
