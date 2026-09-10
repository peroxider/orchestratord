"""agent.model_aliases — requested-model → actually-served-model matching.

ccb-style gateways serve a different model than the label claims (e.g.
haiku label, glm actually served). When ``agent.model_aliases`` maps the
label onto the real model, telemetry/run reports attribute usage to the
actual model and cost is re-estimated from its pricing-table rates —
never from the table ``default``, which would silently mis-price an
unknown actual model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestratord.cost.estimator import (
    estimate_cost_usd,
    native_currency,
    resolve_model_alias,
)


# ── resolve_model_alias ───────────────────────────────────────────────

ALIASES = {"claude-haiku-4": "glm-5.3-flash", "claude-sonnet-4": "glm-5.3"}


def test_alias_exact_match() -> None:
    assert resolve_model_alias("claude-haiku-4", ALIASES) == "glm-5.3-flash"


def test_alias_longest_prefix_match() -> None:
    assert (
        resolve_model_alias("claude-haiku-4-5-20251001", ALIASES)
        == "glm-5.3-flash"
    )


def test_alias_unmatched_passes_through() -> None:
    assert resolve_model_alias("gpt-5", ALIASES) == "gpt-5"


def test_alias_empty_map_or_model_is_noop() -> None:
    assert resolve_model_alias("gpt-5", {}) == "gpt-5"
    assert resolve_model_alias("", ALIASES) == ""


# ── estimate_cost_usd allow_default ──────────────────────────────────


def test_estimate_unknown_model_no_default_returns_none() -> None:
    assert (
        estimate_cost_usd(
            "totally-unknown-model", 1000, 1000, allow_default=False
        )
        is None
    )


def test_estimate_unknown_model_with_default_uses_default() -> None:
    # default $3/$15 per Mtok → 1M in + 1M out = $18.
    est = estimate_cost_usd("totally-unknown-model", 1_000_000, 1_000_000)
    assert est == pytest.approx(3.0 + 15.0)


def test_estimate_glm_5_3_flash_rates() -> None:
    # $0.15 in / $0.50 out per Mtok (repo pricing.json, sourced from
    # Z.ai's list price) — 2M in + 1M out = $0.80.
    est = estimate_cost_usd(
        "glm-5.3-flash", 2_000_000, 1_000_000, allow_default=False
    )
    assert est == pytest.approx(0.15 * 2 + 0.50)


# ── native_currency (原始计价币种标注) ────────────────────────────────


def test_native_currency_cny_for_domestic_models() -> None:
    # Domestic vendors bill in RMB — pricing.json tags their entries cny.
    assert native_currency("glm-5.3-flash") == "cny"
    assert native_currency("deepseek-chat") == "cny"


def test_native_currency_usd_for_overseas_and_untagged() -> None:
    # Untagged/unknown entries report usd — the table's rates are
    # USD-denominated, so absence of a tag means USD billing.
    assert native_currency("claude-sonnet-4") == "usd"
    assert native_currency("totally-unknown-model") == "usd"
    assert native_currency("") == "usd"


# ── config parsing ────────────────────────────────────────────────────


def test_config_parses_model_aliases() -> None:
    from orchestratord.config.schema import WorkflowConfig

    cfg = WorkflowConfig.from_dict(
        {
            "agent": {
                "model": "claude-haiku-4-5",
                "model_aliases": {"claude-haiku-4": "glm-5.3-flash"},
            }
        }
    )
    assert cfg.agent.model == "claude-haiku-4-5"
    assert cfg.agent.model_aliases == {"claude-haiku-4": "glm-5.3-flash"}


def test_config_model_aliases_default_empty() -> None:
    from orchestratord.config.schema import WorkflowConfig

    assert WorkflowConfig.from_dict({}).agent.model_aliases == {}


# ── BackendRunner._resolve_cost ───────────────────────────────────────


def _runner_with(model: str, aliases: dict[str, str]):
    from orchestratord.backend_runner import BackendRunner

    runner = object.__new__(BackendRunner)
    runner.agent_config = SimpleNamespace(model=model, model_aliases=aliases)
    return runner


def test_resolve_cost_reestimates_for_aliased_model() -> None:
    runner = _runner_with(
        "claude-haiku-4-5", {"claude-haiku-4": "glm-5.3-flash"}
    )
    session = SimpleNamespace(
        _snapshot_model="glm-5.3-flash",
        cost_usd=99.0,  # CLI's haiku-priced figure — must be replaced
        token_usage={"input_tokens": 2_000_000, "output_tokens": 1_000_000},
    )
    runner._resolve_cost(session)
    assert session.cost_usd == pytest.approx(0.80)


def test_resolve_cost_keeps_reported_when_unmatched() -> None:
    runner = _runner_with("gpt-5", {})
    session = SimpleNamespace(
        _snapshot_model="gpt-5", cost_usd=0.42, token_usage={}
    )
    runner._resolve_cost(session)
    assert session.cost_usd == 0.42


def test_resolve_cost_keeps_reported_when_no_token_usage() -> None:
    # Alias matched but usage extraction failed (empty/None token_usage):
    # 0 tokens would estimate 0.0 — the reported cost must survive.
    runner = _runner_with(
        "claude-haiku-4-5", {"claude-haiku-4": "glm-5.3-flash"}
    )
    for usage in (None, {}, {"input_tokens": 0, "output_tokens": 0}):
        session = SimpleNamespace(
            _snapshot_model="glm-5.3-flash", cost_usd=4.0, token_usage=usage
        )
        runner._resolve_cost(session)
        assert session.cost_usd == 4.0, f"usage={usage!r}"


def test_resolve_cost_keeps_reported_when_unpriceable(monkeypatch) -> None:
    # An alias pointing at a model absent from the pricing table must
    # keep the reported cost rather than zeroing it or using `default`.
    import orchestratord.cost.estimator as estimator_mod

    monkeypatch.setattr(estimator_mod, "estimate_cost_usd", lambda *a, **kw: None)
    runner = _runner_with("claude-haiku-4", {"claude-haiku-4": "glm-x"})
    session = SimpleNamespace(
        _snapshot_model="glm-x",
        cost_usd=0.42,
        token_usage={"input": 100, "output": 50},
    )
    runner._resolve_cost(session)
    assert session.cost_usd == 0.42
