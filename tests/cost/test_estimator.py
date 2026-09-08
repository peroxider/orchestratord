"""Token cost estimator tests (§7.2) — pure unit, no database.

Pins model-rate matching (exact → longest prefix → default), the
tokens→USD math, table loading (env override → repo file), and cache
reset semantics.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.2.
"""

from __future__ import annotations

import json

import pytest

from orchestratord.cost.estimator import (
    estimate_cost_usd,
    load_pricing,
    reset_pricing_cache,
)

_TABLE = {
    "default": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    "models": {
        "claude-sonnet-4": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        "claude-sonnet-4-5": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
    },
}


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_pricing_cache()
    yield
    reset_pricing_cache()


class TestRateMatching:
    def test_exact_model_id(self) -> None:
        assert estimate_cost_usd("claude-sonnet-4", 1_000_000, 0, _TABLE) == 3.0

    def test_longest_prefix_wins(self) -> None:
        # Date-suffixed ids match the most specific table entry.
        cost = estimate_cost_usd("claude-sonnet-4-5-20250929", 1_000_000, 0, _TABLE)
        assert cost == 5.0

    def test_unknown_model_falls_back_to_default(self) -> None:
        cost = estimate_cost_usd("mystery-model", 1_000_000, 1_000_000, _TABLE)
        assert cost == pytest.approx(3.0 + 15.0)

    def test_no_default_means_none(self) -> None:
        assert estimate_cost_usd("mystery", 10, 10, {"models": {}}) is None

    def test_empty_model_uses_default(self) -> None:
        assert estimate_cost_usd("", 1_000_000, 0, _TABLE) == 3.0


class TestCostMath:
    def test_input_and_output_combined(self) -> None:
        cost = estimate_cost_usd("claude-sonnet-4", 2_000_000, 1_000_000, _TABLE)
        assert cost == pytest.approx(2 * 3.0 + 15.0)

    def test_zero_tokens_zero_cost(self) -> None:
        assert estimate_cost_usd("claude-sonnet-4", 0, 0, _TABLE) == 0.0


class TestLoadPricing:
    def test_env_override_wins(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "pricing.json"
        path.write_text(
            json.dumps({"default": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}})
        )
        monkeypatch.setenv("ORCHESTRATORD_PRICING_JSON", str(path))
        table = load_pricing()
        assert table["default"]["input_per_mtok"] == 1.0

    def test_repo_table_loads_with_known_models(self, monkeypatch) -> None:
        monkeypatch.delenv("ORCHESTRATORD_PRICING_JSON", raising=False)
        table = load_pricing()
        assert "claude-sonnet-4" in table["models"]
        assert table["default"]["output_per_mtok"] > 0

    def test_estimate_against_repo_table(self, monkeypatch) -> None:
        monkeypatch.delenv("ORCHESTRATORD_PRICING_JSON", raising=False)
        cost = estimate_cost_usd("claude-sonnet-4-20250929", 1_000_000, 1_000_000, None)
        assert cost == pytest.approx(3.0 + 15.0)

    def test_env_override_points_at_missing_file_falls_back(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("ORCHESTRATORD_PRICING_JSON", str(tmp_path / "missing.json"))
        table = load_pricing()
        assert "default" in table
