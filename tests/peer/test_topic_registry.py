"""Unit tests for the PR-B9 per-peer frame EVENT topic registry."""

from __future__ import annotations

import pytest

from orchestratord.peer import topic_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    topic_registry.reset_peer_topics()
    yield
    topic_registry.reset_peer_topics()


def test_add_and_get_topics() -> None:
    topic_registry.add_peer_topic("orch-A", "peer.session.x")
    topic_registry.add_peer_topic("orch-A", "peer.inbox.y")
    assert topic_registry.get_peer_topics("orch-A") == {
        "peer.session.x",
        "peer.inbox.y",
    }


def test_non_peer_topics_are_ignored() -> None:
    topic_registry.add_peer_topic("orch-A", "session.abc")
    topic_registry.add_peer_topic("orch-A", "issues")
    assert topic_registry.get_peer_topics("orch-A") == set()


def test_topics_are_isolated_per_peer() -> None:
    topic_registry.add_peer_topic("orch-A", "peer.a")
    topic_registry.add_peer_topic("orch-B", "peer.b")
    assert topic_registry.get_peer_topics("orch-A") == {"peer.a"}
    assert topic_registry.get_peer_topics("orch-B") == {"peer.b"}


def test_remove_topic_is_noop_when_absent() -> None:
    topic_registry.remove_peer_topic("orch-A", "peer.nope")  # no peer entry
    topic_registry.add_peer_topic("orch-A", "peer.a")
    topic_registry.remove_peer_topic("orch-A", "peer.nope")
    assert topic_registry.get_peer_topics("orch-A") == {"peer.a"}


def test_clear_peer_topics_drops_only_that_peer() -> None:
    topic_registry.add_peer_topic("orch-A", "peer.a")
    topic_registry.add_peer_topic("orch-B", "peer.b")
    topic_registry.clear_peer_topics("orch-A")
    assert topic_registry.get_peer_topics("orch-A") == set()
    assert topic_registry.get_peer_topics("orch-B") == {"peer.b"}


def test_get_returns_a_copy() -> None:
    topic_registry.add_peer_topic("orch-A", "peer.a")
    topics = topic_registry.get_peer_topics("orch-A")
    topics.add("peer.mutable")
    assert topic_registry.get_peer_topics("orch-A") == {"peer.a"}
