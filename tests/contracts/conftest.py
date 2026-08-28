"""Contract test fixtures.

Provides a ``stub_backend`` fixture that creates a fresh StubBackend
for each test, plus helpers for capability-gated test skipping.
"""

from __future__ import annotations

import pytest

from orchestratord.spi.capabilities import BackendCapabilities

from .stub_backend import StubBackend


@pytest.fixture
def stub_backend() -> StubBackend:
    """Fresh StubBackend instance per test (all capabilities enabled)."""
    backend = StubBackend()
    yield backend
    backend.dispose()


def require_capability(caps: BackendCapabilities, attr: str, reason: str = "") -> None:
    """Skip the current test if *caps* does not have *attr* set to True."""
    if not getattr(caps, attr, False):
        pytest.skip(reason or f"backend does not support {attr}")