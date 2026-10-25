#!/usr/bin/env python3
"""Five-hour admission reserves apply to both providers, including with pacing disabled."""

import os
import sys
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tauceti_worker import loop, quota
from tauceti_worker.config import NoProgress


class StartLimitTests(unittest.TestCase):
    def setUp(self):
        self.now = float(int(time.time()))
        self.reset = self.now + 3600
        self.q = quota.Quota.__new__(quota.Quota)
        self.env = patch.dict(os.environ, {"TAUCETI_PACE": "0:100,100:100"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def provider(self, name, session, weekly=10, *, reset=None):
        reset = self.reset if reset is None else reset
        if name == "codex":
            return self.q._codex_from_payload(
                {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": weekly,
                            "limit_window_seconds": 604800,
                            "reset_after_seconds": 86400,
                        },
                        "secondary_window": {
                            "used_percent": session,
                            "limit_window_seconds": 18000,
                            "reset_after_seconds": reset - self.now,
                        },
                    }
                },
                self.now,
            )
        with patch.object(quota.time, "time", return_value=self.now):
            return self.q._claude_from_payload(
                {
                    "five_hour": {
                        "utilization": session,
                        "resets_at": datetime.fromtimestamp(reset, UTC).isoformat(),
                    },
                    "seven_day": {
                        "utilization": weekly,
                        "resets_at": datetime.fromtimestamp(self.now + 86400, UTC).isoformat(),
                    },
                }
            )

    def test_boundary_and_weekly_independence(self):
        for name in ("claude", "codex"):
            for used in (94.99, 95, 99.99, 100):
                with self.subTest(provider=name, used=used):
                    p = self.provider(name, used)
                    self.assertEqual(p.available, used < 95)
                    session = next(w for w in p.windows if w.name == "session")
                    expected = "under-pace" if used < 95 else "exhausted" if used == 100 else "start-limit"
                    self.assertEqual(session.status, expected)
                    if used >= 95:
                        self.assertEqual(p.next_eligible, self.reset)
                        self.assertFalse(quota._unavail_reason(p)[0])
                        self.assertEqual(loop._ignore_quota_verdict(None, p), "wait")
                        if used < 100:
                            self.assertIn("95% start limit", quota.quota_line({name: p}, markup=False))
            self.assertTrue(self.provider(name, 10, weekly=99).available)
            self.assertFalse(self.provider(name, 10, weekly=100).available)
            self.assertTrue(self.provider(name, 0, reset=self.now + 17999).available)

    def test_start_limit_dominates_soft_pacing_and_idle_bootstrap(self):
        with patch.dict(os.environ, {"TAUCETI_PACE": "0:0,100:100"}):
            for name in ("claude", "codex"):
                p = self.provider(name, 95, weekly=90)
                self.assertEqual(loop._ignore_quota_verdict(None, p), "wait")
                self.assertIn("95% start limit", quota._unavail_reason(p)[1])
                below = self.provider(name, 94.99, weekly=90)
                self.assertEqual(loop._ignore_quota_verdict(None, below), "over-pace")
        p = self.q._claude_provider(
            [
                quota.Reading("session", "active", 95, self.reset),
                quota.Reading("weekly", "idle"),
            ],
            now=self.now,
        )
        self.assertFalse(p.bootstrap_eligible)

    def test_auto_can_select_another_provider(self):
        with (
            patch.object(self.q, "codex", return_value=self.provider("codex", 95)),
            patch.object(self.q, "claude", return_value=self.provider("claude", 10)),
        ):
            self.assertEqual(self.q.choose("auto")[0], "claude")

    def test_one_shot_refuses_even_with_ignore_quota(self):
        for name in ("claude", "codex"):
            p = self.provider(name, 95)
            for ignore in (False, True):
                with self.subTest(provider=name, ignore_quota=ignore):
                    with patch.object(loop, "choose_model", return_value=(None, {name: p})):
                        with self.assertRaisesRegex(NoProgress, "95% start limit"):
                            loop.resolve_work_model(object(), name, dry=False, ignore_quota=ignore, fresh=True)

    def test_loop_finishes_active_round_waits_then_starts_after_reset(self):
        for name in ("claude", "codex"):
            for ignore in (False, True):
                with self.subTest(provider=name, ignore_quota=ignore):
                    snapshots = [
                        (name, {name: self.provider(name, 94.99)}),
                        (None, {name: self.provider(name, 95)}),
                        (name, {name: self.provider(name, 0, reset=self.now + 18000)}),
                    ]
                    completed = []

                    def run_round(_tail, completed=completed):
                        if completed:
                            self.assertEqual(choose.call_count, 3)
                            raise KeyboardInterrupt
                        self.assertEqual(choose.call_count, 1)
                        completed.append(True)
                        return 0

                    with (
                        patch.object(loop, "choose_model", side_effect=snapshots) as choose,
                        patch.object(loop, "github_budget", return_value={}),
                        patch.object(loop, "run_round_subprocess", side_effect=run_round) as run,
                        patch.object(loop, "api_mode", return_value=False),
                        patch.object(loop, "report_runtime") as status,
                        patch.object(loop, "runtime_snapshot", return_value={}),
                        patch.object(loop.time, "sleep") as sleep,
                    ):
                        args = SimpleNamespace(ignore_quota=ignore, bubble=False, quota_cmd=None)
                        rc = loop.cmd_loop(args, SimpleNamespace(wid="test"), only=["review"], agent=name)
                    self.assertEqual(rc, 130)
                    self.assertEqual(completed, [True])
                    self.assertEqual(run.call_count, 2)
                    self.assertTrue(sleep.called)
                    waits = [c for c in status.call_args_list if c.args == ("waiting-quota",)]
                    self.assertEqual(len(waits), 1)
                    self.assertIn("95% start limit", waits[0].kwargs["detail"])


if __name__ == "__main__":
    unittest.main()
