#!/usr/bin/env python3
"""The heartbeat identifies the PR, active verification stage and relevant log."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import round as worker_round


class Progress(unittest.TestCase):
    def render(self, **status):
        with (
            patch.object(worker_round, "runtime_snapshot", return_value=status),
            patch.object(worker_round.time, "time", return_value=120),
        ):
            return worker_round._round_status(0)

    def test_verification_shows_phase_elapsed_time_and_tool_log(self):
        line = self.render(
            state="verifying",
            phase="recovery-verify",
            target="PR #19254",
            verification_phase="build",
            verification_started_at=100,
            verification_log="/tmp/build.log",
            round_log="/tmp/previous-agent.log",
        )
        self.assertIn("PR #19254", line)
        self.assertIn("checking build", line)
        self.assertIn(worker_round.console.hms(20), line)
        self.assertIn("/tmp/build.log", line)
        self.assertNotIn("previous-agent", line)

    def test_agent_work_does_not_display_stale_verification(self):
        line = self.render(
            state="working",
            phase="recovery-repair",
            target="PR #19254",
            verification_phase="toolchain",
            verification_log="/tmp/probe.log",
            round_log="/tmp/agent.log",
        )
        self.assertIn("recovery-repair PR #19254", line)
        self.assertIn("/tmp/agent.log", line)
        self.assertNotIn("checking", line)
        self.assertNotIn("probe.log", line)


if __name__ == "__main__":
    unittest.main()
