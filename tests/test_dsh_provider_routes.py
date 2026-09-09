"""dsh custom provider routes (agent.providers → llm-pi-ai cordis).

Covers the three layers of the feature:

* config — ``WorkflowConfig`` parses ``agent.providers`` into
  ``ProviderConfig`` entries (raw api_key preserved);
* generation — ``orchestratord_dsh.cordis_gen`` validates the route
  table, resolves credentials through the environment only, and
  text-appends an ``llm-pi-ai`` plugin block to the bundled default
  cordis config (preserving its ``!!js`` runtime tags byte-for-byte);
* preflight — ``DshBackend.preflight`` accepts declared routes and
  rejects undeclared ones with actionable messages, while the legacy
  deepseek-official path keeps its exact historical behavior.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from orchestratord_dsh.backend import DshBackend

from orchestratord.config.schema import WorkflowConfig
from orchestratord.spi.backend import SessionSpec

cordis_gen = pytest.importorskip(
    "orchestratord_dsh.cordis_gen",
    reason="orchestratord-dsh not installed in this environment",
)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_schema_parses_agent_providers() -> None:
    config, _ = _workflow(
        """
agent:
  providers:
    my-gateway:
      api: openai-completions
      base_url: https://gw.example/v1
      api_key: $MY_GATEWAY_KEY
      models: [kimi-k2]
    deepseek-official:
      api_key: $DEEPSEEK_API_KEY
"""
    )
    providers = config.agent.providers
    assert set(providers) == {"my-gateway", "deepseek-official"}
    gateway = providers["my-gateway"]
    assert gateway.api == "openai-completions"
    assert gateway.base_url == "https://gw.example/v1"
    # Raw: the consuming backend resolves $VAR so an unresolvable
    # reference can be reported with the original variable name.
    assert gateway.api_key == "$MY_GATEWAY_KEY"
    assert gateway.models == ["kimi-k2"]
    assert providers["deepseek-official"].api_key == "$DEEPSEEK_API_KEY"


def test_schema_providers_default_empty_and_malformed_entries_dropped() -> None:
    config, _ = _workflow("agent:\n  model: m\n")
    assert config.agent.providers == {}
    config, _ = _workflow(
        "agent:\n  providers:\n    bad-route: not-a-mapping\n    ok: {}\n"
    )
    assert set(config.agent.providers) == {"ok"}


def _workflow(yaml_text: str) -> tuple[WorkflowConfig, str]:
    import yaml as _yaml

    raw = _yaml.safe_load(yaml_text) or {}
    config = WorkflowConfig.from_dict({"agent": raw.get("agent", {})})
    return config, ""


# ---------------------------------------------------------------------------
# cordis_gen: credential resolution
# ---------------------------------------------------------------------------


def test_route_env_var_name_sanitizes_route() -> None:
    assert cordis_gen.route_env_var_name("my-gateway") == "DSH_ROUTE_MY_GATEWAY_KEY"
    assert cordis_gen.route_env_var_name("a b.c") == "DSH_ROUTE_A_B_C_KEY"


def test_resolve_route_credential_literals_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cordis_gen.resolve_route_credential(None) is None
    assert cordis_gen.resolve_route_credential("") is None
    assert cordis_gen.resolve_route_credential("sk-literal") == "sk-literal"
    monkeypatch.setenv("GW_KEY", "sk-env")
    assert cordis_gen.resolve_route_credential("$GW_KEY") == "sk-env"
    assert cordis_gen.resolve_route_credential("${GW_KEY}") == "sk-env"
    assert cordis_gen.resolve_route_credential("$GW_KEY", {"GW_KEY": "x"}) == "x"


def test_resolve_route_credential_missing_var_names_original_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GW_MISSING", raising=False)
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route_credential("$GW_MISSING")
    assert "$GW_MISSING" in str(raised.value)
    assert "GW_MISSING is not set" in str(raised.value)


# ---------------------------------------------------------------------------
# cordis_gen: validation
# ---------------------------------------------------------------------------


def _route(**overrides: object) -> dict[str, object]:
    route: dict[str, object] = {
        "api": "openai-completions",
        "base_url": "https://gw.example/v1",
        "api_key": "sk-literal",
        "models": ["m1"],
    }
    route.update(overrides)
    return route


def test_validate_providers_accepts_minimal_route() -> None:
    cordis_gen.validate_providers({"gw": _route()})


def test_validate_providers_rejects_empty_table() -> None:
    with pytest.raises(cordis_gen.CordisConfigError):
        cordis_gen.validate_providers({})


def test_validate_providers_rejects_unknown_api() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.validate_providers({"gw": _route(api="grpc-whatever")})
    assert "grpc-whatever" in str(raised.value)
    assert "openai-completions" in str(raised.value)


def test_validate_providers_rejects_route_without_api_and_base_url() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.validate_providers({"gw": _route(api=None, base_url=None)})
    assert "gw" in str(raised.value)


def test_validate_providers_rejects_duplicate_models() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.validate_providers({"gw": _route(models=["m1", "m1"])})
    assert "more than once" in str(raised.value)


def test_validate_providers_rejects_env_name_collision() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.validate_providers({"a-b": _route(), "a_b": _route()})
    assert "a-b" in str(raised.value) and "a_b" in str(raised.value)


def test_validate_providers_does_not_resolve_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential resolution is per-active-route; an unresolvable $VAR on
    a route this run does not use must not block validation."""
    monkeypatch.delenv("GW_UNSET", raising=False)
    cordis_gen.validate_providers({"gw": _route(api_key="$GW_UNSET")})


def test_validate_providers_accepts_static_header_values() -> None:
    """Static (non-sensitive) header values are allowed: they are
    written verbatim to the generated cordis file and sent as-is by
    the runtime (the llm-pi-ai adapter sends ``headers`` values as
    literal strings)."""
    cordis_gen.validate_providers(
        {"gw": _route(headers={"X-Tenant": "acme", "X-Custom": "static-value"})}
    )


def test_validate_providers_rejects_env_referenced_header_values() -> None:
    """Regression (second-round review finding): $VAR-referenced headers
    must be rejected — the runtime's llm-pi-ai profile schema only has
    ``headers: z.dict(z.string())`` (no CredentialRef/headersEnv
    support), so a $VAR header would be sent as a literal string or
    silently dropped. The error must guide the operator: headers only
    support static values; do not put secrets in headers (static values
    land on disk in plaintext); credentials belong in api_key/apiKeyEnv."""
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.validate_providers(
            {"gw": _route(headers={"Authorization": "Bearer $MY_KEY"})}
        )
    message = str(raised.value)
    assert "gw" in message and "Authorization" in message
    assert "$" in message
    assert "apiKeyEnv" in message or "api_key" in message


# ---------------------------------------------------------------------------
# cordis_gen: text generation
# ---------------------------------------------------------------------------


def test_build_cordis_patch_updates_existing_profile_without_copying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GW_KEY", "sk-test")
    text = cordis_gen.build_cordis_text({"my-gateway": _route(api_key="$GW_KEY")})
    # The runtime composes its own profile, including JavaScript tags.
    assert "sdk-jsonrpc-server" not in text
    assert "session-persistence" not in text
    assert "id: llm-pi-ai" in text
    assert "name: '@deepseek-ai/dsh-llm-pi-ai'" in text
    assert "baseURL: https://gw.example/v1" in text
    assert "apiKeyEnv: DSH_ROUTE_MY_GATEWAY_KEY" in text
    assert "- id: deepseek-chat" not in text  # route models, not catalog
    assert "id: m1" in text
    assert "sk-test" not in text


def test_build_cordis_text_keyless_route_omits_api_key_env() -> None:
    text = cordis_gen.build_cordis_text({"anon": _route(api_key=None)})
    route_section = text.split("anon:")[-1]
    assert "apiKeyEnv" not in route_section


def test_build_cordis_text_model_entry_camel_case() -> None:
    text = cordis_gen.build_cordis_text(
        {
            "gw": _route(
                models=[
                    {"id": "big", "context_window": 131072, "max_tokens": 8192},
                ]
            )
        }
    )
    assert "contextWindow: 131072" in text
    assert "maxTokens: 8192" in text


def test_generate_cordis_file_writes_reports_dir_with_run_stem(
    tmp_path,
) -> None:
    path = cordis_gen.generate_cordis_file(
        {"gw": _route()}, tmp_path / ".reports", run_id="stage-01-ab12cd34"
    )
    assert path.parent == tmp_path / ".reports"
    assert path.name == "dsh-cordis-stage-01-ab12cd34.yml"
    assert path.read_text(encoding="utf-8").startswith("# --- orchestratord generated")
    # Concurrent/second run with the same id overwrites deterministically.
    again = cordis_gen.generate_cordis_file(
        {"gw": _route()}, tmp_path / ".reports", run_id="stage-01-ab12cd34"
    )
    assert again == path


def test_generate_cordis_static_headers_written_verbatim(tmp_path) -> None:
    """Static header values pass validation and are written verbatim
    to the generated cordis file — the runtime sends them as literal
    strings (the adapter has no $VAR expansion in headers)."""
    path = cordis_gen.generate_cordis_file(
        {"gw": _route(headers={"X-Tenant": "acme"})},
        tmp_path / ".reports",
        run_id="stage-01",
    )
    text = path.read_text(encoding="utf-8")
    assert "X-Tenant" in text
    assert "acme" in text
    assert "headersEnv" not in text


def test_generate_cordis_rejects_env_referenced_headers(tmp_path) -> None:
    """A $VAR-referenced header value is rejected at validation —
    no headersEnv is generated; the runtime has no CredentialRef
    support for headers."""
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.generate_cordis_file(
            {"gw": _route(headers={"Authorization": "Bearer $MY_KEY"})},
            tmp_path / ".reports",
            run_id="stage-01",
        )
    message = str(raised.value)
    assert "$" in message
    assert "apiKeyEnv" in message or "api_key" in message


# ---------------------------------------------------------------------------
# cordis_gen: route selection
# ---------------------------------------------------------------------------


def test_resolve_route_single_route_and_single_model_auto_selected() -> None:
    provider, model = cordis_gen.resolve_route(
        {"gw": _route()}, None, None, default_model="deepseek-v4-flash"
    )
    assert (provider, model) == ("gw", "m1")


def test_resolve_route_explicit_selection_wins() -> None:
    provider, model = cordis_gen.resolve_route(
        {"gw": _route(models=["m1", "m2"])}, "gw", "m2", default_model="d"
    )
    assert (provider, model) == ("gw", "m2")


def test_resolve_route_multi_route_requires_provider() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route(
            {"a": _route(), "b": _route()}, None, None, default_model="d"
        )
    assert "a, b" in str(raised.value)


def test_resolve_route_multi_model_requires_model() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route(
            {"gw": _route(models=["m1", "m2"])}, "gw", None, default_model="d"
        )
    assert "m1, m2" in str(raised.value)


def test_resolve_route_unknown_provider_lists_declared_routes() -> None:
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route({"gw": _route()}, "nope", None, default_model="d")
    assert "gw" in str(raised.value) and "nope" in str(raised.value)


def test_resolve_route_deepseek_official_falls_back_to_stock_adapter() -> None:
    """deepseek-official stays reachable alongside a registry — the
    runtime auto-mounts its stock adapter and the legacy credential
    chain applies."""
    provider, model = cordis_gen.resolve_route(
        {"gw": _route()},
        "deepseek-official",
        None,
        default_model="deepseek-v4-flash",
    )
    assert (provider, model) == ("deepseek-official", "deepseek-v4-flash")


def test_resolve_route_route_without_models_requires_explicit_model() -> None:
    """Regression (review finding): a registry route with no declared
    models and no agent.model used to fall back to the DeepSeek default,
    silently addressing a foreign gateway with a DeepSeek model id.
    The selection must instead fail with an actionable message."""
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route(
            {"deepseek": {"base_url": None}}, "deepseek", None,
            default_model="deepseek-chat",
        )
    assert "declares no models" in str(raised.value)
    # The deepseek-official fallback path keeps its default (unchanged).
    provider, model = cordis_gen.resolve_route(
        {"gw": _route()}, "deepseek-official", None,
        default_model="deepseek-v4-flash",
    )
    assert (provider, model) == ("deepseek-official", "deepseek-v4-flash")


# ---------------------------------------------------------------------------
# Preflight: route path
# ---------------------------------------------------------------------------


def _spec_with_routes(
    providers: dict[str, dict] | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    cordis: str | None = None,
) -> SessionSpec:
    extra = {"providers": providers} if providers is not None else {}
    return SessionSpec(
        cwd="/workspace",
        provider=provider,
        model=model,
        cordis=cordis,
        extra=extra,
    )


@pytest.fixture
def patched_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "orchestratord_dsh.backend.probe_llm_pi_ai_available", lambda: True
    )


@pytest.fixture
def gw_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_KEY", "sk-test")


def test_preflight_accepts_declared_route(gw_key, patched_probe) -> None:
    spec = _spec_with_routes(
        {"my-gateway": _route(api_key="$GW_KEY")},
        provider="my-gateway",
        model="m1",
    )
    DshBackend().preflight(spec)  # must not raise


def test_preflight_single_route_auto_selected(gw_key, patched_probe) -> None:
    spec = _spec_with_routes({"my-gateway": _route(api_key="sk-literal")})
    DshBackend().preflight(spec)  # must not raise


def test_preflight_rejects_undeclared_provider(gw_key, patched_probe) -> None:
    spec = _spec_with_routes({"my-gateway": _route()}, provider="openai")
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    message = str(raised.value)
    assert "openai" in message and "my-gateway" in message


def test_preflight_multi_route_requires_provider(gw_key, patched_probe) -> None:
    spec = _spec_with_routes({"a": _route(), "b": _route()})
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "a, b" in str(raised.value)


def test_preflight_multi_model_requires_model(gw_key, patched_probe) -> None:
    spec = _spec_with_routes(
        {"gw": _route(models=["m1", "m2"])}, provider="gw"
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "m1" in str(raised.value)


def test_preflight_rejects_model_outside_route_declaration(gw_key, patched_probe) -> None:
    spec = _spec_with_routes(
        {"gw": _route()}, provider="gw", model="not-declared"
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "not-declared" in str(raised.value)


def test_preflight_rejects_unresolvable_route_credential(
    monkeypatch: pytest.MonkeyPatch, patched_probe
) -> None:
    monkeypatch.delenv("GW_UNSET", raising=False)
    spec = _spec_with_routes(
        {"gw": _route(api_key="$GW_UNSET")}, provider="gw"
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "$GW_UNSET" in str(raised.value)


def test_preflight_rejects_env_referenced_header(
    monkeypatch: pytest.MonkeyPatch, patched_probe,
) -> None:
    """A $VAR-referenced header is rejected at preflight (via
    validate_providers) with guidance text — never silently dropped
    by the runtime."""
    monkeypatch.setenv("MY_HEADER_KEY", "sk-header")
    spec = _spec_with_routes(
        {"gw": _route(headers={"Authorization": "Bearer $MY_HEADER_KEY"})},
        provider="gw",
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    message = str(raised.value)
    assert "$" in message
    assert "apiKeyEnv" in message or "api_key" in message


def test_preflight_accepts_static_header(patched_probe) -> None:
    spec = _spec_with_routes(
        {"gw": _route(headers={"X-Tenant": "acme"})},
        provider="gw",
    )
    DshBackend().preflight(spec)  # must not raise


def test_preflight_cordis_and_providers_are_mutually_exclusive(gw_key, patched_probe) -> None:
    spec = _spec_with_routes(
        {"gw": _route()}, provider="gw", cordis="/tmp/custom-cordis.yml"
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "mutually exclusive" in str(raised.value)


def test_preflight_keyless_route_passes_credential_check(patched_probe) -> None:
    """A route with no api_key is legitimate (keyless gateway / ambient
    discovery) — preflight must not demand DEEPSEEK_API_KEY for it."""
    spec = SessionSpec(cwd="/workspace", extra={"providers": {"anon": _route(api_key=None)}})
    DshBackend().preflight(spec)  # must not raise


def test_preflight_deepseek_official_uses_legacy_credential_chain(
    monkeypatch: pytest.MonkeyPatch, patched_probe
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    spec = _spec_with_routes(
        {"gw": _route()}, provider="deepseek-official", model="deepseek-v4-flash"
    )
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "DEEPSEEK_API_KEY" in str(raised.value)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    DshBackend().preflight(spec)  # must not raise


def test_preflight_probe_failure_names_the_plugin(monkeypatch: pytest.MonkeyPatch, gw_key) -> None:
    monkeypatch.setattr(
        "orchestratord_dsh.backend.probe_llm_pi_ai_available", lambda: False
    )
    spec = _spec_with_routes({"gw": _route(api_key="$GW_KEY")}, provider="gw")
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "llm-pi-ai" in str(raised.value)


# ---------------------------------------------------------------------------
# Session-side route resolution
# ---------------------------------------------------------------------------


def test_session_resolver_uses_registry_without_explicit_selection() -> None:
    from orchestratord_dsh.session import DshSession

    spec = _spec_with_routes({"my-gateway": _route()})
    session = DshSession(spec)
    assert session._resolve_provider_model() == ("my-gateway", "m1")


def test_session_resolver_legacy_path_unchanged() -> None:
    from orchestratord_dsh.session import DshSession

    spec = SessionSpec(cwd="/workspace")
    session = DshSession(spec)
    assert session._resolve_provider_model() == ("deepseek-official", "deepseek-v4-flash")

    spec = SessionSpec(cwd="/workspace", provider="deepseek-official", model="deepseek-chat")
    session = DshSession(spec)
    assert session._resolve_provider_model() == ("deepseek-official", "deepseek-chat")


def test_session_factory_generates_cordis_and_injects_credential(
    monkeypatch: pytest.MonkeyPatch, gw_key, tmp_path
) -> None:
    """The default harness factory must hand the SDK a generated cordis
    path plus the route credential as an environment variable — and
    never write the secret into the config file."""
    captured: dict[str, object] = {}

    class _FakeHarness:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(
        "deepseek_harness.api.DeepSeekHarness", _FakeHarness
    )

    from orchestratord_dsh.session import DshSession

    spec = SessionSpec(
        cwd=str(tmp_path),
        provider="my-gateway",
        extra={"providers": {"my-gateway": _route(api_key="$GW_KEY")}},
        run_id="stage-01-feedface",
    )
    session = DshSession(spec)
    session._default_harness_factory()

    assert captured["started"] is True
    config = captured["config"]
    cordis_path = config.patches[0]
    assert cordis_path and "dsh-cordis-stage-01-feedface.yml" in str(cordis_path)
    generated_files = list((tmp_path / ".reports").glob("*.yml"))
    assert len(generated_files) == 1
    assert "sk-test" not in generated_files[0].read_text()
    env = getattr(config, "env", {})
    assert env["DSH_ROUTE_MY_GATEWAY_KEY"] == "sk-test"
    assert config.provider == "my-gateway"
    assert config.model == "m1"


def test_session_factory_static_headers_land_in_cordis_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Static route headers are written verbatim into the generated
    cordis config (the adapter sends ``headers`` values as literal
    strings); no header env vars are injected into the child
    environment."""
    captured: dict[str, object] = {}

    class _FakeHarness:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(
        "deepseek_harness.api.DeepSeekHarness", _FakeHarness
    )

    from orchestratord_dsh.session import DshSession

    spec = SessionSpec(
        cwd=str(tmp_path),
        provider="gw",
        extra={"providers": {"gw": _route(
            headers={"X-Tenant": "acme"}
        )}},
        run_id="stage-01-feedface",
    )
    session = DshSession(spec)
    session._default_harness_factory()

    assert captured["started"] is True
    config = captured["config"]
    env = getattr(config, "env", {})
    # No header env vars are injected (no headersEnv mechanism exists).
    assert not any(k.startswith("DSH_ROUTE_GW_HEADER_") for k in env)
    generated_files = list((tmp_path / ".reports").glob("*.yml"))
    assert len(generated_files) == 1
    text = generated_files[0].read_text()
    assert "X-Tenant" in text
    assert "acme" in text
    assert "headersEnv" not in text


@pytest.mark.parametrize("reference", ["$DSH_TEST_KEY", "${DSH_TEST_KEY}"])
@pytest.mark.parametrize("session_override", [False, True])
def test_legacy_credential_reference_matches_native_child_environment(
    monkeypatch, tmp_path, reference, session_override
) -> None:
    from deepseek_harness.api import DeepSeekHarness
    from orchestratord_dsh.session import DshSession

    monkeypatch.setenv("DSH_TEST_KEY", "test-parent-key")
    monkeypatch.setattr(DeepSeekHarness, "start", lambda self: None)
    spec = SessionSpec(
        cwd=str(tmp_path),
        api_key=reference,
        env={"DSH_TEST_KEY": "test-session-key"} if session_override else {},
    )
    DshBackend().preflight(spec)
    harness = DshSession(spec)._default_harness_factory()
    assert harness.client.config.env["DEEPSEEK_API_KEY"] == (
        "test-session-key" if session_override else "test-parent-key"
    )


def test_legacy_preflight_accepts_session_only_credential(monkeypatch, tmp_path) -> None:
    from deepseek_harness.api import DeepSeekHarness
    from orchestratord_dsh.session import DshSession

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(DeepSeekHarness, "start", lambda self: None)
    spec = SessionSpec(cwd=str(tmp_path), env={"DEEPSEEK_API_KEY": "test-session-key"})
    DshBackend().preflight(spec)
    harness = DshSession(spec)._default_harness_factory()
    assert harness.client.config.env["DEEPSEEK_API_KEY"] == "test-session-key"


def test_legacy_preflight_rejects_credential_masked_in_session(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-parent-key")
    spec = SessionSpec(cwd="/workspace", env={"DEEPSEEK_API_KEY": ""})
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DshBackend().preflight(spec)


def test_route_credential_uses_session_environment_without_persisting_secret(
    monkeypatch, tmp_path, patched_probe
) -> None:
    from deepseek_harness.api import DeepSeekHarness
    from orchestratord_dsh.session import DshSession

    monkeypatch.delenv("DSH_TEST_ROUTE_KEY", raising=False)
    monkeypatch.setattr(DeepSeekHarness, "start", lambda self: None)
    spec = SessionSpec(
        cwd=str(tmp_path),
        provider="gw",
        env={"DSH_TEST_ROUTE_KEY": "test-session-route-key"},
        extra={"providers": {"gw": _route(api_key="$DSH_TEST_ROUTE_KEY")}},
    )
    DshBackend().preflight(spec)
    harness = DshSession(spec)._default_harness_factory()
    assert harness.client.config.env["DSH_ROUTE_GW_KEY"] == "test-session-route-key"
    assert "test-session-route-key" not in Path(harness.config.patches[0]).read_text()


def test_custom_cordis_owns_nonstock_provider_and_survives_permission_overlay(
    monkeypatch, tmp_path
) -> None:
    from deepseek_harness.api import DeepSeekHarness
    from orchestratord_dsh.session import DshSession

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(DeepSeekHarness, "start", lambda self: None)
    custom = tmp_path / "oauth-provider.yml"
    custom.write_text("- insert:\n    - id: test-oauth\n      name: test-oauth-plugin\n")
    spec = SessionSpec(
        cwd=str(tmp_path), provider="test-oauth", model="test-mini",
        cordis=str(custom), permission_mode="bypassPermissions",
    )
    DshBackend().preflight(spec)
    harness = DshSession(spec)._default_harness_factory()
    assert harness.config.provider == "test-oauth"
    assert harness.config.model == "test-mini"
    assert harness.config.patches[0] == str(custom)
    assert len(harness.config.patches) == 2
    assert "policy: never" in Path(harness.config.patches[1]).read_text()
    assert "test-oauth-plugin" in custom.read_text()


def test_resolve_route_explicit_unknown_provider_is_rejected() -> None:
    """An explicitly configured provider that is not a declared route
    must be reported — 'anthropic' is no longer special-cased: the
    schema default is empty, so an anthropic value here is a real
    (mis)configuration."""
    with pytest.raises(cordis_gen.CordisConfigError) as raised:
        cordis_gen.resolve_route({"gw": _route()}, "anthropic", None, default_model="d")
    assert "anthropic" in str(raised.value) and "gw" in str(raised.value)


def test_schema_provider_default_is_empty_so_dsh_is_usable_out_of_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: AgentConfig.provider defaulted to 'anthropic', which
    no dsh runtime adapter serves — a dsh workflow without
    agent.provider died at preflight. The default is now empty and each
    backend applies its own (dsh → deepseek-official stock adapter)."""
    config = WorkflowConfig.from_dict({})
    assert config.agent.provider == ""
    # Legacy path: empty provider + credential → preflight passes.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    DshBackend().preflight(SessionSpec(cwd="/workspace"))  # must not raise


def test_providers_extra_wraps_registry_under_providers_key() -> None:
    """Regression: the daemon-startup preflight once passed the route
    map as ``extra`` itself (no ``providers`` wrapper), so preflight
    silently took the legacy path and rejected the undeclared default
    provider. Both spec-build sites must go through this helper."""
    from orchestratord.backend_runner import providers_extra
    from orchestratord.config.schema import ProviderConfig

    assert providers_extra(object()) == {}
    agent = SimpleNamespace(
        providers={"gw": ProviderConfig(api="openai-completions", models=["m"])}
    )
    extra = providers_extra(agent)
    assert set(extra) == {"providers"}
    assert extra["providers"]["gw"]["api"] == "openai-completions"


def test_session_factory_parks_sdk_transcripts_outside_git_tree(
    monkeypatch: pytest.MonkeyPatch, gw_key, tmp_path
) -> None:
    """Regression: the dsh SDK persisted session.jsonl.zstd under
    <workspace>/.sessions, which git-sync's ``git add -A`` committed
    into PRs. The harness must park persistence under .reports/."""
    captured: dict[str, object] = {}

    class _FakeHarness:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def start(self) -> None:
            pass

    monkeypatch.setattr("deepseek_harness.api.DeepSeekHarness", _FakeHarness)

    from orchestratord_dsh.session import DshSession

    spec = _spec_with_routes({"my-gateway": _route(api_key="$GW_KEY")})
    spec.cwd = str(tmp_path)
    DshSession(spec)._default_harness_factory()
    session_root = captured["config"].dsh_home
    assert session_root == str(tmp_path / ".reports" / "dsh-home")
    assert ".sessions" not in str(session_root)


def test_gitignore_defaults_exclude_harness_session_persistence() -> None:
    """Both ignore lists (git-sync side and workspace-exclude side) must
    name .sessions so agent-internal transcripts never reach a PR."""
    from unittest.mock import Mock

    from orchestratord.config.schema import WorkflowConfig
    from orchestratord.git.sync import GitSyncService

    git_sync = GitSyncService(tracker=Mock())
    assert ".sessions" in git_sync._gitignore_patterns

    config = WorkflowConfig.from_dict({})
    assert ".sessions" in config.workspace.gitignore_patterns


def test_schema_forwards_agent_python_executable() -> None:
    """Regression: agent.python_executable existed on the dataclass but
    from_dict never forwarded the YAML value — workflow config was
    silently ignored (the interpreter hint never reached the prompt)."""
    config, _ = _workflow("agent:\n  python_executable: /opt/py311/bin/python\n")
    assert config.agent.python_executable == "/opt/py311/bin/python"
    config2, _ = _workflow("agent:\n  model: m\n")
    assert config2.agent.python_executable == ""


# ---------------------------------------------------------------------------
# Permission preset wiring (bypassPermissions → danger-full-access)
# ---------------------------------------------------------------------------


def test_permission_mode_to_approval_policy_mapping() -> None:
    from orchestratord_dsh.cordis_gen import permission_mode_to_approval_policy

    assert permission_mode_to_approval_policy("bypassPermissions") == "never"
    assert permission_mode_to_approval_policy("bypasspermissions") == "never"
    assert permission_mode_to_approval_policy(None) is None
    assert permission_mode_to_approval_policy("") is None
    # Unmapped / restrictive modes keep the runtime default ("ask") —
    # fail-closed.
    assert permission_mode_to_approval_policy("dontAsk") is None
    assert permission_mode_to_approval_policy("default") is None


def test_build_cordis_text_approval_block_only() -> None:
    text = cordis_gen.build_cordis_text(None, approval_policy="never")
    assert "name: '@deepseek-ai/dsh-user-approval'" in text
    assert "policy: never" in text
    assert "dsh-llm-pi-ai" not in text  # no provider routes requested
    assert "id: approval" in text
    assert "session-persistence" not in text


def test_autonomous_approval_presets_preserve_all_sandbox_modes() -> None:
    import yaml

    entries = yaml.safe_load(cordis_gen.build_approval_block("never"))
    permission = next(entry for entry in entries if entry["id"] == "permission")
    presets = permission["config"]["presets"]
    assert {p["sandbox"] for p in presets.values()} == {
        "read-only", "workspace-write", "danger-full-access",
    }
    assert all(p["approval"] == "never" for p in presets.values())
    assert "defaultPreset" not in permission["config"]


def test_build_cordis_text_providers_and_approval_together() -> None:
    text = cordis_gen.build_cordis_text(
        {"gw": _route()}, approval_policy="never"
    )
    assert "dsh-llm-pi-ai" in text
    assert "policy: never" in text


def test_build_cordis_text_nothing_to_generate_raises() -> None:
    with pytest.raises(cordis_gen.CordisConfigError):
        cordis_gen.build_cordis_text(None, approval_policy=None)


def test_session_factory_generates_cordis_for_bypass_permissions_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """bypassPermissions WITHOUT a provider registry must still generate
    a cordis (approval block only) — the stock deepseek-official
    adapter path was previously cordis-free."""
    captured: dict[str, object] = {}

    class _FakeHarness:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def start(self) -> None:
            pass

    monkeypatch.setattr("deepseek_harness.api.DeepSeekHarness", _FakeHarness)

    from orchestratord_dsh.session import DshSession

    spec = SessionSpec(cwd=str(tmp_path), permission_mode="bypassPermissions")
    DshSession(spec)._default_harness_factory()

    config = captured["config"]
    cordis_path = config.patches[0]
    assert cordis_path and "dsh-cordis-" in str(cordis_path)
    text = Path(cordis_path).read_text()
    assert "policy: never" in text
    assert "dsh-llm-pi-ai" not in text


def test_session_factory_no_cordis_for_restrictive_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    class _FakeHarness:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def start(self) -> None:
            pass

    monkeypatch.setattr("deepseek_harness.api.DeepSeekHarness", _FakeHarness)

    from orchestratord_dsh.session import DshSession

    spec = SessionSpec(cwd=str(tmp_path), permission_mode="dontAsk")
    DshSession(spec)._default_harness_factory()
    assert captured["config"].patches == ()


# ---------------------------------------------------------------------------
# Preflight probe: custom runtime_bin gate (review finding)
# ---------------------------------------------------------------------------


def test_preflight_custom_runtime_bin_skips_inconclusive_probe(
    monkeypatch: pytest.MonkeyPatch, gw_key
) -> None:
    """The bundled-exe scan can only inspect the bundled runtime: a
    custom agent.runtime_bin makes the probe INCONCLUSIVE — it must
    skip, not fail (the probe returning False for a perfectly good
    custom build was a false-negative preflight)."""
    monkeypatch.setattr(
        "orchestratord_dsh.backend.probe_llm_pi_ai_available", lambda: False
    )
    spec = _spec_with_routes({"gw": _route(api_key="$GW_KEY")}, provider="gw")
    spec.runtime_bin = "/opt/custom-dsh-runtime"
    DshBackend().preflight(spec)  # must not raise


def test_preflight_bundled_runtime_probe_failure_still_blocks(monkeypatch, gw_key) -> None:
    """Without a custom runtime_bin the probe is authoritative."""
    monkeypatch.setattr(
        "orchestratord_dsh.backend.probe_llm_pi_ai_available", lambda: False
    )
    spec = _spec_with_routes({"gw": _route(api_key="$GW_KEY")}, provider="gw")
    with pytest.raises(RuntimeError) as raised:
        DshBackend().preflight(spec)
    assert "llm-pi-ai" in str(raised.value)
