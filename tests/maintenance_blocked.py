"""Worker blocker policy works with shared surveys that omit the blocked projection."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.review_state import ReviewState
from tauceti_worker.survey_facts import blocked

HEAD = "a" * 40
MARKER = f"<!--tauceti-blocked:v1 {HEAD}-->\nEvidence"


class MaintenanceBlockers(unittest.TestCase):
    def test_markers_require_an_unquoted_header_and_comment_actor(self):
        valid = {"body": MARKER, "user": {"login": "Author"}}
        self.assertEqual(blocked([valid]), [{"head": HEAD, "author": "author"}])
        for comment in (
            None,
            {"body": MARKER},
            {**valid, "body": "> " + MARKER},
            {**valid, "body": "Quoted\n" + MARKER},
            {**valid, "body": MARKER + "x" * 32768},
        ):
            with self.subTest(comment=repr(comment)[:80]):
                self.assertEqual(blocked([comment]), [])
        self.assertEqual(blocked(None), [])

    def test_selected_pr_checks_comments_when_compact_projection_is_absent(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TAUCETI_GITHUB_STATE_URL": "https://state.test"}),
        ):
            gh = SimpleNamespace(issue_comments=Mock(return_value=[{"body": MARKER, "user": {"login": "Author"}}]))
            state = ReviewState(SimpleNamespace(sbcache=Path(tmp)), gh)
            state.seed_survey([{"pr": {"number": 1}, "review": {"issue_complete": True}}])
            self.assertFalse(state.maintenance_blocked(1, HEAD, "Author"))
            gh.issue_comments.assert_not_called()
            self.assertTrue(state.maintenance_blocked(1, HEAD, "Author", selected=True))
            gh.issue_comments.assert_called_once_with(1)
            self.assertTrue(state.maintenance_blocked(1, HEAD, "Author"))
            self.assertFalse(state.maintenance_blocked(1, "b" * 40, "Author"))
            self.assertFalse(state.maintenance_blocked(1, HEAD, "someone-else"))

    def test_existing_compact_projection_needs_no_comment_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh = SimpleNamespace(issue_comments=Mock(side_effect=AssertionError("unexpected read")))
            state = ReviewState(SimpleNamespace(sbcache=Path(tmp)), gh)
            state.seed_survey([{"pr": {"number": 1}, "review": {"blocked": [{"head": HEAD, "author": "author"}]}}])
            self.assertTrue(state.maintenance_blocked(1, HEAD, "Author"))


if __name__ == "__main__":
    unittest.main()
