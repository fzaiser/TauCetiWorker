#!/usr/bin/env python3
"""The loop stops, rather than backing off, when the next round cannot do better than the last.

Backing off suits a round that found nothing or waited on a provider. It does not suit an error every
round on this host repeats: each retry re-spends the survey and launches the same failure at the next
PR in the queue. One such loop ran 13 rounds in three hours, all failing identically, charging nine
PRs along the way.

Pinned here:

  1. An unmanaged loop gets a status file in its state dir, so rounds can hand back failure details.
  2. A round reporting scope "machine" stops the loop at once, with EX_STUCK.
  3. The same error ending LOOP_REPEAT_FAILURE_LIMIT rounds in a row stops it; PR numbers and paths in
     the reason do not make the errors different.
  4. Distinct errors, no-progress rounds and timeouts do not count; a productive round resets the count.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker.loop as loop
from tauceti_worker.constants import ALLOWED_TASKS, EX_NOPROGRESS, EX_STUCK, LOOP_REPEAT_FAILURE_LIMIT
from tauceti_worker.runtime_status import STATUS_ENV, read_json, update_status

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


loop.choose_model = lambda *_a, **_k: ("codex", {})
loop.github_budget = lambda: {}
loop.time = SimpleNamespace(time=time.time, sleep=lambda _s: None, strftime=time.strftime)


def run(script, state, only=("review",)):
    """Drive cmd_loop through `script`, a list of (rc, reason, scope[, phase[, ctrl_c_presses]]) rounds;
    the loop is interrupted once the script runs out. Returns (loop rc, rounds run, argv tails)."""
    rounds = iter(script)
    ran = 0
    tails = []

    def fake_round(tail):
        nonlocal ran
        try:
            rc, reason, scope, *rest = next(rounds)
        except StopIteration:
            raise KeyboardInterrupt from None
        phase = rest[0] if rest else "review"
        presses = rest[1] if len(rest) > 1 else 0
        ran += 1
        tails.append(list(tail))
        update_status(
            Path(os.environ[STATUS_ENV]),
            phase=phase,
            failure_reason=reason,
            failure_code=rc if reason else None,
            failure_scope=scope,
        )
        for _ in range(presses):
            os.kill(os.getpid(), signal.SIGINT)
        return rc

    loop.run_round_subprocess = fake_round
    os.environ.pop(STATUS_ENV, None)
    args = SimpleNamespace(ignore_quota=False, bubble=False, quota_cmd=None)
    cfg = SimpleNamespace(wid="t", state=state)
    return loop.cmd_loop(args, cfg, only=list(only), agent="codex"), ran, tails


def only_of(tail):
    return tail[tail.index("--only") + 1].split(",") if "--only" in tail else None


def main():
    limit = LOOP_REPEAT_FAILURE_LIMIT
    crash = "review #{} exited with status 1: AttributeError: module 'os' has no attribute 'waitid'"
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)

        # 1) the status file.
        rc, ran, _ = run([(0, None, None)], state)
        status = state / "runtime-status.json"
        check("an interrupted loop exits 130", rc == 130)
        check("an unmanaged loop writes a status file in its state dir", status.is_file())
        check("the loop reports it is stopping", read_json(status).get("state") == "stopping")

        # 2) scope machine stops at once.
        rc, ran, _ = run(
            [(EX_NOPROGRESS, "preflight: the review engine cannot run here", "machine"), (0, None, None)], state
        )
        check("a machine-scope failure stops the loop", rc == EX_STUCK)
        check("it stops after that one round", ran == 1)
        check("the status says why", "cannot run here" in str(read_json(status).get("detail")))

        # 3) the same error `limit` times in a row stops the loop, PR numbers notwithstanding.
        script = [(1, crash.format(100 + n), None) for n in range(limit + 2)]
        rc, ran, _ = run(script, state)
        check("repeated identical errors stop the loop", rc == EX_STUCK)
        check(f"after exactly {limit} rounds", ran == limit)

        # 4) what does not count.
        distinct = [(1, f"error {chr(97 + n)}: something else", None) for n in range(limit + 1)]
        rc, ran, _ = run(distinct, state)
        check("distinct errors do not stop the loop", rc == 130 and ran == limit + 1)

        # A no-progress round between two identical errors neither counts nor resets: the queue being
        # empty for a round says nothing about whether the next PR would fail the same way.
        mixed = [(1, crash.format(1), None), (EX_NOPROGRESS, "no eligible work this round", None)] * (limit - 1)
        rc, ran, _ = run(mixed, state)
        check("no-progress rounds do not count", rc == 130 and ran == 2 * (limit - 1))
        rc, ran, _ = run(mixed + [(1, crash.format(2), None)], state)
        check("nor do they reset the count", rc == EX_STUCK and ran == 2 * (limit - 1) + 1)

        timeouts = [(124, None, None)] * (limit + 1)
        rc, ran, _ = run(timeouts, state)
        check("timeouts do not count", rc == 130 and ran == limit + 1)

        reset = (
            [(1, crash.format(1), None)] * (limit - 1) + [(0, None, None)] + [(1, crash.format(2), None)] * (limit - 1)
        )
        rc, ran, _ = run(reset, state)
        check("a productive round resets the count", rc == 130 and ran == len(reset))

        # 5) a stage that keeps failing is dropped; the others go on. Successful rounds of OTHER
        # stages do not reset its count (reviews between broken roadmap rounds are what happened).
        luna = "codex agent exited with status 1: The 'gpt-6-luna' model is not supported"
        script = [(1, luna, None, "roadmap"), (0, None, None, "review")] * limit + [(0, None, None, "review")]
        rc, ran, tails = run(script, state, only=())
        check("a failing stage does not stop the loop while others work", rc == 130 and ran == len(script))
        check("the cascade was unrestricted before", only_of(tails[0]) is None)
        after = only_of(tails[-1])
        check("the failing stage is dropped from later rounds", after is not None and "roadmap" not in after)
        check("every other stage stays", after is not None and set(after) == set(ALLOWED_TASKS) - {"roadmap"})

        # A machine-scope failure of one stage drops that stage at once.
        script = [
            (EX_NOPROGRESS, "codex: neither gpt-6-sol nor gpt-6-luna is available", "machine", "fix"),
            (0, None, None, "review"),
        ]
        rc, ran, tails = run(script, state, only=("fix", "review"))
        check("a machine-scope stage failure drops the stage at once", rc == 130 and only_of(tails[-1]) == ["review"])

        # 6) Ctrl-C: one press finishes the round, a second stops at once.
        rc, ran, _ = run([(0, None, None, "review", 1), (0, None, None, "review")], state)
        check("one Ctrl-C stops after the current round, with status 0", rc == 0 and ran == 1)
        rc, ran, _ = run([(0, None, None, "review", 2), (0, None, None, "review")], state)
        check("a second Ctrl-C stops the round at once, with status 130", rc == 130 and ran == 1)
        check("the loop restores the SIGINT handler", signal.getsignal(signal.SIGINT) is signal.default_int_handler)

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
