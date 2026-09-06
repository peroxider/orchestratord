"""Mention parsing invariants (§7.4).

``parse_mentions`` extracts ``@handle`` tokens from comment text without
resolving them to agents/members. Invariants:

* Handles are returned unique, in first-seen order.
* Emails (``a@b.com``) and doubled ``@@`` are not mentions.
* Sentence punctuation terminates a handle; ``-`` / ``_`` are allowed.
* Empty text yields no mentions.

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.4.
"""

from __future__ import annotations

from orchestratord.domain.mention import parse_mentions


class TestExtraction:
    def test_single_mention(self) -> None:
        assert parse_mentions("@alice please look") == ["alice"]

    def test_multiple_in_order(self) -> None:
        assert parse_mentions("@alice and @bob review") == ["alice", "bob"]

    def test_deduplicates(self) -> None:
        assert parse_mentions("@alice @alice @bob") == ["alice", "bob"]

    def test_mention_at_start(self) -> None:
        assert parse_mentions("@alice do it") == ["alice"]

    def test_no_mentions(self) -> None:
        assert parse_mentions("nothing to see here") == []

    def test_empty_text(self) -> None:
        assert parse_mentions("") == []


class TestBoundaries:
    def test_email_not_a_mention(self) -> None:
        assert parse_mentions("mail me at foo@bar.com") == []

    def test_doubled_at_not_a_mention(self) -> None:
        assert parse_mentions("@@alice") == []

    def test_trailing_punctuation_stripped(self) -> None:
        assert parse_mentions("@alice, and @bob!") == ["alice", "bob"]

    def test_dash_and_underscore_handles(self) -> None:
        assert parse_mentions("@agent-name and @member_name") == [
            "agent-name",
            "member_name",
        ]

    def test_at_inside_word_not_a_mention(self) -> None:
        assert parse_mentions("handle@alice") == []
