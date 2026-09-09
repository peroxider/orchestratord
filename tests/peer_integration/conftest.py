"""Fixtures for the cross-process peer federation integration tests."""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "database: requires a live PostgreSQL (scratch DBs); "
        "skipped when unreachable",
    )
