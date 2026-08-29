"""Fragment tests for the git rebase service (rebuilt from transcripts)."""

import tempfile
import unittest
from pathlib import Path


class TestAheadBehind(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_ab_"))
        self.path = self.tmp / "repo"
        self.path.mkdir()


class TestRebaseForPr(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_rebase_"))
        self.path = self.tmp / "repo"
        self.path.mkdir()


class TestGitRebaseAbort(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_abort_"))
        self.path = self.tmp / "repo"
        self.path.mkdir()
