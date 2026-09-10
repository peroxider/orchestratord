"""Tests for privacy.scrub (DESIGN_EXPERIENCE_LOOP.md §8.2)."""

from orchestratord.privacy import scrub


def test_masks_api_token_shapes():
    text = "use sk-abcdef1234567890abcdef for auth and ghp_" + "a" * 30
    out = scrub(text)
    assert "sk-abcdef1234567890abcdef" not in out
    assert "ghp_" + "a" * 30 not in out
    assert "***REDACTED_TOKEN***" in out


def test_masks_key_value_secrets():
    out = scrub("api_key = 'supersecretvalue123'")
    assert "supersecretvalue123" not in out
    assert "***REDACTED***" in out
    # The key name itself survives.
    assert "api_key" in out


def test_masks_internal_urls():
    out = scrub(
        "dashboard at http://192.168.1.50:8080/admin and "
        "http://grafana.internal:3000/dash and http://localhost:9222/devtools"
    )
    assert "192.168.1.50" not in out
    assert "grafana.internal" not in out
    assert "localhost:9222" not in out
    assert out.count("***REDACTED_INTERNAL_URL***") == 3


def test_masks_emails_and_user_paths():
    out = scrub(
        "ping alice@example.com; log at /home/chad/x.log; see C:\\Users\\chad\\tmp"
    )
    assert "alice@example.com" not in out
    assert "/home/chad/" not in out
    assert "chad\\tmp" not in out
    assert "***REDACTED_EMAIL***" in out


def test_allowlist_exempts_literal_substring():
    url = "http://metrics.internal:9090/metrics"
    assert scrub(url) == "***REDACTED_INTERNAL_URL***"
    assert scrub(url, allowlist=["metrics.internal"]) == url


def test_clean_text_passes_through():
    text = "retry with exponential backoff, then re-run tests"
    assert scrub(text) == text
