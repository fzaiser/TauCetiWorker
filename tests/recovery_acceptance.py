#!/usr/bin/env python3
"""Local recovery acceptance with an installed worker, real Lean and local Git.

Run with the installed worker's Python and --lean-bin pointing at an installed
Lean toolchain's bin directory. No toolchain downloads or public writes occur.
Build and shim checks are real; GitHub, provider admission and paid authoring are
local fixtures. Mathlib-dependent audits remain covered by their separate tests.
"""

import argparse
import contextlib
import functools
import importlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_tools.templates import python_wrapper

from tauceti_worker import agents
from tauceti_worker import repair_recovery as rr
from tauceti_worker import work_recovery as saved
from tauceti_worker.verification import Verification

# Load the installed package before the local fixture adds the source tree to sys.path.
fixture = importlib.import_module("repair_recovery")
Fixture, git = fixture.Fixture, fixture.git


class Acceptance(Fixture):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(
            os.environ,
            {
                "PATH": str(LEAN_BIN) + os.pathsep + os.environ["PATH"],
                "HOME": str(self.root),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
                "TAUCETI_FLEET_EVENTS": str(self.root / "events.jsonl"),
                "TAUCETI_VERIFY_CACHE_DIR": str(self.root / "cache"),
                "TAUCETI_VERIFY_STORAGE_DIR": str(self.root / "storage"),
                "TAUCETI_VERIFY_MIN_FREE_BYTES": "1",
                "TAUCETI_VERIFY_STATE": str(self.cfg.state),
                "TAUCETI_RUNTIME_STATUS": str(self.root / "runtime.json"),
                "TAUCETI_VERIFY_HELPER": "1",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.good_source = "theorem fixture_value : (2 : Nat) = 2 := rfl\n"
        (self.co / "TauCeti").mkdir()
        (self.co / "TauCeti.lean").write_text("")
        (self.co / "TauCeti/Fixture.lean").write_text("theorem fixture_value : (1 : Nat) = 1 := rfl\n")
        (self.co / "lakefile.toml").write_text(
            'name = "fixture"\ndefaultTargets = ["TauCeti"]\n[[lean_lib]]\nname = "TauCeti"\nglobs = ["TauCeti.*"]\n'
        )
        (self.co / "scripts").mkdir()
        (self.co / "scripts/check-expired-mathlib-shims.py").write_text(
            python_wrapper("check-expired-mathlib-shims.py")
        )
        (self.co / "TauCeti/mathlib-shims.json").write_text("[]\n")
        subprocess.run([str(LEAN_BIN / "lake"), "update"], cwd=self.co, check=True, capture_output=True)
        git(self.co, "add", "-A")
        git(self.co, "commit", "-qm", "local Lean fixture")
        self.base = git(self.co, "rev-parse", "HEAD")
        git(self.co, "push", "-q", "origin", "main")
        git(self.remote, "update-ref", "refs/pull/10/head", self.base)
        self.current["head"] = self.base
        self.owner.update(round_id="real-lean", expected_head=self.base)
        (self.co / "TauCeti/Fixture.lean").write_text("theorem fixture_value : (1 : Nat) = 2 := by decide\n")
        self.record = saved._record(
            self.cfg, self.co, saved.inspect(self.co), "TauCeti", "upstream/repo", owner=self.owner
        )
        self.manifest = saved._complete(self.record)
        self.reset_backend()
        for mocked in (
            patch("tauceti_worker.verification.Verification", functools.partial(Verification, stage="build")),
            patch.object(agents, "warm_host_build_caches"),
            patch.object(agents, "TAUCETI_CACHE_SERVICE", ""),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def reset_backend(self):
        self.backend = rr.WorkerBackend(self.w, self.opts)
        publish = self.backend.publish

        def local_publish(*args):
            publish(*args)
            self.current["head"] = git(self.remote, "rev-parse", "main")

        self.backend.publish = local_publish

    def installed_verify(self, checkout, stage):
        result = subprocess.run(
            [sys.executable, "-P", "-m", "tauceti_worker", "verify", "--stage", stage, "--base", self.base, "--json"],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
        )
        receipt = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0 if receipt["status"] == "success" else 1, result.stderr)
        return receipt

    def test_authentication_wait_restart_and_real_repair(self):
        started = time.monotonic()
        with patch(
            "tauceti_worker.loop.resolve_work_model", side_effect=RuntimeError("HTTP 401: refresh authentication")
        ):
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.journal()["reason"]["code"], "budget-refused")
        self.assertIn("HTTP 401", self.journal()["reason"]["detail"])
        self.assertEqual(self.journal().get("content_attempts", 0), 0)
        self.assertEqual(self.journal()["failed_receipt"]["failed_phase"], "build")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        self.retry_now()
        self.reset_backend()
        repair = f"from pathlib import Path; Path('TauCeti/Fixture.lean').write_text({self.good_source!r})"
        with (
            patch("tauceti_worker.loop.resolve_work_model", return_value=("claude", False)),
            patch("tauceti_worker.work_units.raise_on_account_mismatch"),
            patch.object(rr, "conversation_identity", return_value="fixture-account"),
            patch.object(
                agents, "host_agent_argv", side_effect=lambda *a: ([sys.executable, "-c", repair], os.environ.copy())
            ),
            patch.object(agents, "host_build_cache_context", side_effect=lambda *a: contextlib.nullcontext([])),
            patch("tauceti_worker.fleet_capacity.check_launch_admission"),
            patch.object(agents, "run_agent_host", side_effect=REAL_AGENT) as model,
        ):
            self.assertTrue(self.run_recovery())
        model.assert_called_once()
        self.assertEqual(git(self.remote, "show", "main:TauCeti/Fixture.lean") + "\n", self.good_source)
        self.assertEqual(self.journal()["state"], "published")
        self.assertEqual(self.values["fix-10-head"], 1)
        receipt = self.installed_verify(self.record / "checkout", "build")
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertIn("reused_from", receipt["phases"]["build"])
        self.assertLess(time.monotonic() - started, 60, "tiny recovery should reach useful work promptly")

    def test_unavailable_cache_preserves_source_without_model(self):
        with patch.object(agents, "warm_host_build_caches", side_effect=agents.BuildCacheUnavailable("cache offline")):
            self.assertFalse(self.run_recovery())
        self.assertIn("cache offline", self.journal()["reason"]["detail"])
        self.assertEqual(self.journal().get("content_attempts", 0), 0)
        self.assertEqual(saved._complete(self.record), self.manifest)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_installed_shim_wrapper_and_checker_crash(self):
        receipt = self.installed_verify(self.co, "shim-expiry")
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        (self.co / "scripts/check-expired-mathlib-shims.py").write_text("raise RuntimeError('checker broken')\n")
        receipt = self.installed_verify(self.co, "shim-expiry")
        self.assertEqual(receipt["status"], "incomplete", receipt.get("error"))
        self.assertEqual(receipt["incomplete_reason"], "tool-failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lean-bin", required=True, type=Path)
    args = parser.parse_args()
    LEAN_BIN = args.lean_bin.resolve()
    if not all((LEAN_BIN / tool).is_file() for tool in ("lean", "lake")):
        parser.error("--lean-bin must contain installed lean and lake executables")
    REAL_AGENT = agents.run_agent_host
    print(f"Worker: {rr.__file__}\nPython: {sys.executable}\nLean: {LEAN_BIN}", flush=True)
    suite = unittest.TestSuite(Acceptance(name) for name in Acceptance.__dict__ if name.startswith("test_"))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
