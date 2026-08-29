"""Fragment tests for the orchestrator CLI (rebuilt from transcripts)."""

import os
import tempfile
import unittest
from unittest.mock import patch


class TestRunRebase(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patcher = patch.dict(
            os.environ, {"ORCHESTRATORD_WORKSPACE_ROOT": tempfile.mkdtemp(prefix="test_ws_")}
        )
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()
