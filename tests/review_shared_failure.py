#!/usr/bin/env python3
"""A review failure that two unrelated PRs share in a row is the host's, and neither PR pays for it.

The review-error budget retires a PR after three engine rounds that error without posting a verdict,
and opens a public "Review stuck" issue for it. When the engine itself cannot start — a Python without
os.waitid, a reviewer binary gone from PATH, a rejected credential — every PR the loop picks errors the
same way, and the budget retires the queue one PR at a time for a fault on the operator's machine.
One afternoon of that charged nine PRs.

Pinned here:

  1. The first failure is charged (it cannot yet be told from a genuine PR failure) and remembered.
  2. The same failure on a different PR refunds the first PR, charges nothing, retains no per-PR
     record, and raises NoProgress with scope "machine" (an engine crash needs an operator).
  3. A third identical failure charges nothing and refunds nothing further.
  4. A different failure starts a new streak and is charged.
  5. Categories that are the PR's own (a stale head) stay charged even when repeated across PRs.
  6. An outage (a GitHub rate limit) is shared the same way but with scope "transient".
  7. A posted review ends the streak: the next failure is charged again.
  8. The same PR failing twice is charged twice — a PR's own repeats are its own.
  9. failure_key ignores PR numbers, shas, paths and counts; drop_last_review_attempt forgets one.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker.review_diagnostics import drop_last_review_attempt, failure_key, read_review_failure

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


class Counters:
    def __init__(self):
        self.v = {}

    def read(self, k):
        return self.v.get(k, 0)

    def incr(self, k):
        self.v[k] = self.v.get(k, 0) + 1
        return self.v[k]

    def write(self, k, n):
        self.v[k] = n


ENGINE_CRASH = """\
PR #{pr} head: ed90d1722128
Traceback (most recent call last):
  File "/x/site-packages/runner/pr_diff.py", line 150, in _git
    os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)
AttributeError: module 'os' has no attribute 'waitid'. Did you mean: 'waitpid'?
tauceti-review: could not compute the diff of PR #{pr} (1)
"""
STALE_HEAD = "tauceti-review: expected head {head} but PR #{pr} is at 1234567890ab\n"
RATE_LIMIT = "gh: API rate limit exceeded for user ID 1 (PR #{pr})\n"


def worker(state, counters):
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(state=state, logdir=state / "logs", wid="w", store_dir=state / "store"),
        counters=counters,
        rs=types.SimpleNamespace(bust=lambda pr: None, review_rounds=lambda pr, c: 0),
        gh=types.SimpleNamespace(add_reaction=lambda i: True, remove_reaction=lambda i: True),
    )


def drive(w, pr, rc, log_text=""):
    """Run do_review with the engine stubbed to write `log_text` and exit `rc`; return (raised, rc)."""
    c = types.SimpleNamespace(pr=pr, head="a" * 40, contest=None, contest_reply_id=None)
    opts = types.SimpleNamespace(work_model="codex")

    def fake_engine(argv, logf, label):
        logf.parent.mkdir(parents=True, exist_ok=True)
        logf.write_text(log_text.format(pr=pr, head="a" * 40))
        return rc

    orig = tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me
    tc.work_units.run_to_logfile = fake_engine
    tc.work_units._sync_review_outbox = lambda *a, **k: 0
    tc.work_units.me = lambda: "tester"
    try:
        return None, tc.work_units.do_review(w, types.SimpleNamespace(), c, opts, bubble=False)
    except tc.config.NoProgress as e:
        return e, None
    finally:
        tc.work_units.run_to_logfile, tc.work_units._sync_review_outbox, tc.work_units.me = orig


def main():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        (state / "store").mkdir()
        counters = Counters()
        w = worker(state, counters)

        # 1) the first failure is charged and remembered.
        raised, rc = drive(w, 1, 1, ENGINE_CRASH)
        check("first failure does not raise", raised is None and rc == 1)
        check("first failure is charged", counters.read("review-err-1") == 1)
        check("first failure is retained for the PR", len(read_review_failure(state, 1).get("attempts", [])) == 1)

        # 2) the same failure on another PR: refund, no charge, no record, stop the loop.
        raised, _ = drive(w, 2, 1, ENGINE_CRASH)
        check("shared failure raises NoProgress", raised is not None)
        check("shared engine crash has scope machine", getattr(raised, "scope", None) == "machine")
        check("shared failure names both PRs", raised is not None and "#1" in str(raised) and "#2" in str(raised))
        check("the first PR is refunded", counters.read("review-err-1") == 0)
        check("the first PR's retained attempt is dropped", not read_review_failure(state, 1))
        check("the second PR is not charged", counters.read("review-err-2") == 0)
        check("the second PR retains no failure", not read_review_failure(state, 2))

        # 3) a third one charges and refunds nothing further.
        raised, _ = drive(w, 3, 1, ENGINE_CRASH)
        check("third shared failure raises too", raised is not None and raised.scope == "machine")
        check("third shared failure charges nothing", counters.read("review-err-3") == 0)
        check("nothing goes negative", all(v >= 0 for v in counters.v.values()))

        # 4) a different failure starts a new streak: charged.
        raised, _ = drive(w, 4, 1, STALE_HEAD)
        check("a different failure is charged", raised is None and counters.read("review-err-4") == 1)

        # 5) a PR-specific category stays charged even when another PR repeats it.
        raised, _ = drive(w, 5, 1, STALE_HEAD)
        check("a stale head on the next PR is its own failure", raised is None)
        check("stale heads are both charged", counters.read("review-err-4") == 1 and counters.read("review-err-5") == 1)

        # 6) an outage is shared with scope transient.
        drive(w, 6, 1, RATE_LIMIT)
        raised, _ = drive(w, 7, 1, RATE_LIMIT)
        check("a shared rate limit raises NoProgress", raised is not None)
        check("a shared rate limit has scope transient", getattr(raised, "scope", None) == "transient")
        check("the rate-limited PR is refunded", counters.read("review-err-6") == 0)

        # 7) a posted review ends the streak.
        raised, rc = drive(w, 8, 0)
        check("a posted review succeeds", raised is None and rc == 0)
        raised, _ = drive(w, 9, 1, ENGINE_CRASH)
        check("after a posted review the next failure is charged again", raised is None)
        check("it is charged to its PR", counters.read("review-err-9") == 1)

        # 8) the same PR failing twice is charged twice.
        raised, _ = drive(w, 9, 1, ENGINE_CRASH)
        check("a PR's own repeat is not shared", raised is None)
        check("a PR's own repeat is charged", counters.read("review-err-9") == 2)

        # 9) helpers.
        same = failure_key("tauceti-review: could not compute the diff of PR #9943 (1)") == failure_key(
            "tauceti-review: could not compute the diff of PR #10243 (1)"
        )
        check("failure_key ignores the PR number", same)
        check(
            "failure_key ignores shas and paths",
            failure_key("fatal: /tmp/abc123/x at deadbeef0000 failed")
            == failure_key("fatal: /tmp/zz/y at 0123456789ab failed"),
        )
        check("failure_key keeps the words", failure_key("AttributeError: x") != failure_key("ValueError: x"))
        drop_last_review_attempt(state, 9)
        check("drop_last_review_attempt keeps the earlier attempt", len(read_review_failure(state, 9)["attempts"]) == 1)
        drop_last_review_attempt(state, 9)
        check("drop_last_review_attempt removes the last one", not read_review_failure(state, 9))

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
