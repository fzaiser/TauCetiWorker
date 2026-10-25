#!/usr/bin/env python3
"""Codex windows start only after a reserved launch-stage request, then fresh usage gates work."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tauceti_worker as tc

WEEK = tc.WEEK_WINDOW_S
SESSION = tc.SESSION_WINDOW_S
BOOTSTRAP_REQUEST = tc.Quota._bootstrap_request


def window(used=0, length=WEEK, remaining=None):
    return {
        "used_percent": used,
        "limit_window_seconds": length,
        "reset_after_seconds": length if remaining is None else remaining,
    }


def payload(primary=None, secondary=None, *, limited=False):
    return {
        "rate_limit": {
            "limit_reached": limited,
            "primary_window": window() if primary is None else primary,
            "secondary_window": secondary,
        }
    }


class StopLoop(Exception):
    pass


class CodexBootstrap(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "TAUCETI_PACE": "",
                "TAUCETI_ACTIVITY_DIR": str(self.home / "activity"),
                "TAUCETI_FLEET_EVENTS": str(self.home / "events.jsonl"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (self.home / ".codex").mkdir()
        self.auth = self.home / ".codex" / "auth.json"
        self.auth.write_text(json.dumps({"tokens": {"access_token": "test-token", "account_id": "test-account"}}))
        self.cfg = self.config("first")
        self.q = tc.Quota(self.cfg)
        self.opened = False
        self.before = payload()
        self.after = payload(window(0, remaining=WEEK - 60))
        self.code = 200
        self.requests = []
        self.fetches = 0
        http = patch.object(tc.quota, "_http_get_json", side_effect=self.endpoint)
        http.start()
        self.addCleanup(http.stop)
        request = patch.object(tc.Quota, "_bootstrap_request", autospec=True, side_effect=self.request)
        self.boot = request.start()
        self.addCleanup(request.stop)

    def config(self, wid):
        state = self.home / "state" / wid
        return tc.Config(
            wid=wid,
            home=self.home,
            data_home=self.home,
            state=state,
            checkout=self.home / "co",
            store_dir=self.home / "store",
            sbcache=state / "cache" / "scoreboard",
            logdir=self.home / "logs",
            quota_cache=state / "cache",
        )

    def endpoint(self, url, headers, timeout=15):
        self.assertEqual(url, tc.CODEX_USAGE_URL)
        self.fetches += 1
        return self.code, copy.deepcopy(self.after if self.opened else self.before), None

    def request(self, quota, provider):
        self.assertEqual(provider, "codex")
        records = quota._read_bootstrap_ledger(provider=provider)
        self.assertTrue(records)
        self.assertTrue(all(r["state"] == "in-progress" for r in records.values()))
        self.requests.append(provider)
        self.opened = True
        return True, "ok"

    def test_first_request_starts_clock_and_fresh_usage_allows_work(self):
        for _ in range(3):
            prov = self.q.codex(refresh=True)
            self.assertFalse(prov.available)
            self.assertTrue(prov.bootstrap_eligible)
            self.assertIsNone(prov.windows[0].resets_at)
        self.assertFalse(self.requests)
        self.assertEqual(tc.resolve_work_model(self.cfg, "codex", dry=False, ignore_quota=False), ("codex", True))
        self.assertFalse(self.requests)
        before = self.fetches
        self.assertTrue(self.q.authorize_launch("codex").available)
        self.assertEqual(self.fetches, before + 2)
        self.assertEqual(self.requests, ["codex"])
        self.assertTrue(self.q.authorize_launch("codex").available)
        self.assertEqual(self.requests, ["codex"])

    def test_idle_snapshot_is_not_cached_even_with_legacy_entry(self):
        self.q.codex()
        self.assertFalse((self.cfg.quota_cache / "quota-codex.json").exists())
        self.q._store_raw("codex", self.before, "test-account", time.time() + WEEK)
        self.assertIsNone(self.q._cached_codex("test-account"))

    def test_all_idle_windows_share_one_initialization(self):
        self.before = payload(window(0, SESSION), window())
        self.after = payload(window(0, SESSION, SESSION - 60), window(0, remaining=WEEK - 60))
        self.assertEqual(self.q.codex().pending_bootstrap, ["session", "weekly"])
        self.assertTrue(self.q.authorize_launch("codex").available)
        self.assertEqual(self.requests, ["codex"])
        self.assertEqual(len(self.q._read_bootstrap_ledger(provider="codex")), 2)

    def test_sibling_pacing_and_hard_limits_gate_initialization(self):
        for sibling, limited in (
            (window(90, SESSION, SESSION - 60), False),
            (window(95, SESSION, 60), False),
            (window(100, remaining=60), False),
            ({"used_percent": 0}, False),
            (None, True),
        ):
            for ignore in (False, True):
                with self.subTest(sibling=sibling, limited=limited, ignore=ignore):
                    self.before = payload(secondary=sibling, limited=limited)
                    self.opened = False
                    q = tc.Quota(self.config(f"sibling-{len(self.requests)}-{ignore}"))
                    if sibling and sibling.get("used_percent") == 90 and ignore:
                        self.assertTrue(q.authorize_launch("codex", ignore_quota=True).available)
                    else:
                        count = len(self.requests)
                        self.assertFalse(q.authorize_launch("codex", ignore_quota=ignore).available)
                        self.assertEqual(len(self.requests), count)
            # Each scenario has its own idle episode; keep the shared ledger out of the next case.
            self.q._bootstrap_paths(provider="codex")[0].unlink(missing_ok=True)

    def test_zero_budget_plateau_requires_explicit_override(self):
        with patch.dict(os.environ, {"TAUCETI_PACE": "0:0,90:0,100:100"}):
            prov = self.q.authorize_launch("codex")
            self.assertFalse(prov.bootstrap_eligible)
            self.assertIn("pace budget stays 0%", tc._unavail_reason(prov)[1])
            self.assertFalse(self.requests)
            self.q.authorize_launch("codex", ignore_quota=True)
            self.assertEqual(self.requests, ["codex"])

    def test_fresh_usage_controls_dispatch(self):
        for after, ignore, allowed in (
            (self.after, False, True),
            (payload(window(10, remaining=WEEK - 60)), False, False),
            (payload(window(10, remaining=WEEK - 60)), True, True),
            (payload(window(100, remaining=WEEK - 60)), True, False),
            (payload(window(95, SESSION, SESSION - 60)), True, False),
            (payload({"used_percent": 0}), True, False),
            (payload(), True, False),
        ):
            with self.subTest(after=after, ignore=ignore):
                self.opened = False
                self.after = after
                self.q._forget_raw("codex")
                self.q._bootstrap_paths(provider="codex")[0].unlink(missing_ok=True)
                worker = tc.Worker(self.cfg, None, None, tc.Counters(self.cfg), None, None)
                opts = tc.RoundOpts(
                    only=["review"],
                    agent="codex",
                    work_model="codex",
                    sandbox_host=True,
                    dry_run=False,
                    quota_bootstrap=True,
                    ignore_quota=ignore,
                )
                with (
                    patch.object(tc.work_units, "_host_agent_binary", return_value=None),
                    patch.object(tc.work_units, "_still_actionable", return_value=True),
                    patch.object(tc.work_units, "do_review", return_value=0) as run,
                ):
                    args = ("review", worker, tc.Survey(worker_id=self.cfg.wid), tc.Candidate(1, "head", ""), opts)
                    if allowed:
                        self.assertEqual(tc.work_units.dispatch(*args), 0)
                    else:
                        with self.assertRaises(tc.NoProgress):
                            tc.work_units.dispatch(*args)
                    self.assertEqual(run.call_count, int(allowed))

    def test_no_work_does_not_initialize(self):
        worker = tc.Worker(self.cfg, None, None, tc.Counters(self.cfg), None, None)
        opts = tc.RoundOpts(
            only=["review"],
            agent="codex",
            work_model="codex",
            sandbox_host=True,
            dry_run=False,
            quota_bootstrap=True,
        )
        with patch.object(tc.work_units, "survey", return_value=tc.Survey(worker_id=self.cfg.wid)):
            with self.assertRaisesRegex(tc.NoProgress, "no eligible work"):
                tc.run_round(worker, opts)
        self.assertFalse(self.requests)

    def test_loop_passes_authorization_after_github_preflight(self):
        for remaining in (0, 5000):
            with (
                self.subTest(remaining=remaining),
                patch.object(
                    tc.loop, "time", SimpleNamespace(time=time.time, sleep=lambda _: (_ for _ in ()).throw(StopLoop()))
                ),
                patch.object(tc.loop, "github_budget", return_value={"core": (remaining, int(time.time()) + 60)}),
                patch.object(tc.loop, "run_round_subprocess", side_effect=StopLoop) as spawn,
                self.assertRaises(StopLoop),
            ):
                tc.cmd_loop(
                    SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None),
                    self.cfg,
                    only=["review"],
                    agent="codex",
                )
            self.assertEqual(spawn.call_count, int(remaining > 0))
            if remaining:
                tail = spawn.call_args.args[0]
                self.assertIn("--quota-bootstrap", tail)
                self.assertNotIn("--ignore-quota", tail)
            self.assertFalse(self.requests)

    def test_auto_selection_prefers_ready_provider_then_initializable_codex(self):
        ready = tc.Provider("claude", True, "opus")
        with patch.object(tc.Quota, "claude", return_value=ready):
            self.assertEqual(tc.resolve_work_model(self.cfg, "auto", dry=False, ignore_quota=False), ("claude", False))
        idle = tc.Provider("claude", False, None, bootstrap_eligible=True)
        with patch.object(tc.Quota, "claude", return_value=idle):
            self.assertEqual(tc.resolve_work_model(self.cfg, "auto", dry=False, ignore_quota=False), ("codex", True))
        self.assertFalse(self.requests)

    def test_still_idle_blocks_other_workers_and_token_rotation(self):
        self.after = self.before
        self.assertFalse(self.q.authorize_launch("codex").available)
        self.auth.write_text(json.dumps({"tokens": {"access_token": "rotated", "account_id": "test-account"}}))
        other = tc.Quota(self.config("second"))
        prov = other.authorize_launch("codex")
        self.assertFalse(prov.bootstrap_available)
        self.assertIn("bootstrap attempted", tc._unavail_reason(prov)[1])
        self.assertEqual(self.requests, ["codex"])
        self.assertNotEqual(other._bootstrap_paths(provider="claude"), other._bootstrap_paths(provider="codex"))

    def test_isolated_workers_share_reservation_source(self):
        source = self.home / "source"
        source.mkdir()
        (source / "auth.json").write_text(self.auth.read_text())
        paths = []
        for name in ("isolated-a", "isolated-b"):
            isolated = self.home / name
            isolated.mkdir()
            (isolated / ".tauceti-creds-source").write_text(str(source))
            with patch.dict(os.environ, {"CODEX_HOME": str(isolated)}):
                self.after = self.before
                self.q.authorize_launch("codex")
                paths.append(self.q._bootstrap_paths(provider="codex"))
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(self.requests, ["codex"])

    def test_concurrent_reservations_have_one_winner(self):
        key = self.q._episode_key("weekly", provider="codex")

        def reserve(index):
            return tc.Quota(self.config(str(index)))._reserve_bootstrap([key], provider="codex")[0]

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve, range(4))), 1)

    def test_crash_and_failed_request_do_not_license_repeated_requests(self):
        self.boot.side_effect = RuntimeError("crash")
        with self.assertRaisesRegex(RuntimeError, "crash"):
            self.q.authorize_launch("codex")
        self.assertFalse(tc.Quota(self.config("second")).authorize_launch("codex").bootstrap_available)
        self.assertEqual(self.boot.call_count, 1)
        self.q._bootstrap_paths(provider="codex")[0].unlink()
        self.boot.side_effect = lambda *_: (False, "test failure")
        prov = self.q.authorize_launch("codex")
        self.assertIn("bootstrap failed: test failure", tc._unavail_reason(prov)[1])
        self.q.authorize_launch("codex")
        self.assertEqual(self.boot.call_count, 2)

    def test_endpoint_failure_and_unwritable_ledger_never_spend(self):
        for code in (401, 429, 503):
            self.code = code
            self.assertFalse(tc.Quota(self.config(str(code))).authorize_launch("codex").available)
        self.assertFalse(self.requests)
        self.code = 200
        path = self.q._bootstrap_paths(provider="codex")[0].parent
        path.write_text("not a directory")
        self.assertFalse(self.q.authorize_launch("codex").available)
        self.assertFalse(self.requests)

    def test_request_is_isolated_and_subscription_authenticated(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "other-account",
                "CODEX_API_KEY": "other-account",
                "OPENAI_BASE_URL": "https://example.invalid",
            },
        ):
            argv, env, cwd = self.q._bootstrap_spec("codex")
        self.addCleanup(lambda: Path(cwd).rmdir())
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(argv[-1], tc.QUOTA_BOOTSTRAP_PROMPT)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CODEX_HOME"], str(self.auth.parent))
        self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_request_timeout_and_failure_clean_up_temporary_directory(self):
        for outcome, expected in (
            (subprocess.CompletedProcess([], 0, "ok", ""), (True, "ok")),
            (subprocess.CompletedProcess([], 1, "", "refused"), (False, "codex exited 1: refused")),
            (subprocess.TimeoutExpired("codex", 120), (False, "codex timed out after 120s")),
        ):
            with self.subTest(outcome=outcome), patch.object(tc.quota.subprocess, "run") as run:
                if isinstance(outcome, Exception):
                    run.side_effect = outcome
                else:
                    run.return_value = outcome
                self.assertEqual(BOOTSTRAP_REQUEST(self.q, "codex"), expected)
                self.assertEqual(run.call_args.kwargs["timeout"], tc.QUOTA_BOOTSTRAP_TIMEOUT_S)
                self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())


if __name__ == "__main__":
    unittest.main()
