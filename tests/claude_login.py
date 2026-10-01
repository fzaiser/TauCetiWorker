#!/usr/bin/env python3
"""A Claude login belongs to one config dir, and the worker says which dir needs one.

On macOS the operator's `claude auth login` writes a Keychain item tied to the operator's config dir.
An isolated worker runs `claude` under its own $CLAUDE_CONFIG_DIR, which that login does not cover,
so its bootstrap turn fails with Claude Code's own "Not logged in" while the pacer, reading the
operator's item, sees a credential. The loop used to report the wait and nothing else.

Pinned here:

  1. claude_login_status asks the CLI under the given dir and reads its JSON answer; a missing CLI
     or an unreadable answer is None, not an exception.
  2. The quota-wait hint, on "Not logged in", tells the operator to log in as themselves (workers
     mirror that credential), and stays silent for other errors and for Codex.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker.loop as loop
import tauceti_worker.quota as quota
from tauceti_worker.quota import Provider, claude_login_hint, claude_login_status

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def main():
    config_dir = Path("/tmp/worker1/.claude")
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs.get("env", {}).get("CLAUDE_CONFIG_DIR")))
        return subprocess.CompletedProcess(argv, 0, stdout=fake_run.answer, stderr="")

    saved = quota.subprocess.run
    quota.subprocess.run = fake_run
    try:
        fake_run.answer = '{"loggedIn": true, "authMethod": "claude.ai"}\n'
        check("a logged-in dir reads True", claude_login_status(config_dir) is True)
        check(
            "the CLI is asked under that config dir",
            seen[-1] == (["claude", "auth", "status", "--json"], str(config_dir)),
        )
        fake_run.answer = '{"loggedIn": false, "authMethod": "none"}\n'
        check("a dir without a login reads False", claude_login_status(config_dir) is False)
        fake_run.answer = "not json"
        check("an unreadable answer is None", claude_login_status(config_dir) is None)
        fake_run.answer = '{"loggedIn": true}\n'
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        check("the operator's environment is asked as it is", claude_login_status(None) is True and seen[-1][1] is None)

        def missing(argv, **kwargs):
            raise FileNotFoundError("claude")

        quota.subprocess.run = missing
        check("a missing CLI is None, not an error", claude_login_status(config_dir) is None)
    finally:
        quota.subprocess.run = saved

    not_logged_in = Provider(
        "claude", False, None, error="session bootstrap failed: claude exited 1: Not logged in · Please run /login"
    )
    saved_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        hint = loop._credential_hint("claude", not_logged_in)
        check(
            "the hint tells the operator to log in as themselves",
            "claude auth login" in hint and claude_login_hint() in hint,
        )
        check("the hint never asks for a per-worker login", "CLAUDE_CONFIG_DIR=" not in hint)
        check(
            "other Claude errors get no login hint",
            loop._credential_hint("claude", Provider("claude", False, None, error="usage HTTP 503")) == "",
        )
        check(
            "Codex is not told to log Claude in",
            "claude auth login"
            not in loop._credential_hint("codex", Provider("codex", False, None, error="Not logged in")),
        )
    finally:
        if saved_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved_dir

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
