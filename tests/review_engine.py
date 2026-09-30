#!/usr/bin/env python3
"""The review engine runs pinned, on an interpreter it can run on, and is checked before it is trusted.

`uvx --from git+…/TauCetiReview` tracked the engine's `main` and let uvx pick any interpreter its
metadata admitted. An engine push then reached every worker at its next round untested, and one that
could only run on some interpreters (os.waitid is missing from CPython on macOS before 3.13) failed
every review on the hosts uvx chose wrongly for — charged, each time, to the PR of the round.

Pinned here:

  1. engine_argv runs the pinned revision from a per-revision uvx cache, with the interpreter
     constraint when the platform needs one, and honours $TAUCETI_REVIEW_ENGINE_DIR.
  2. engine_selfcheck reports the interpreter on success, the last line of stderr on failure, and a
     launch error without raising; a local checkout is not checked.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker.review_engine as re_mod
from tauceti_worker.constants import REVIEW, REVIEW_PYTHON, REVIEW_REF

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def main():
    state = Path("/worker-state")
    os.environ.pop("TAUCETI_REVIEW_ENGINE_DIR", None)

    # 1) argv shape.
    check("the pin is a full commit sha", len(REVIEW_REF) == 40 and all(c in "0123456789abcdef" for c in REVIEW_REF))
    argv = re_mod.engine_argv(state, "42", "--store", "/s")
    check("uvx runs the engine", argv[0] == "uvx")
    check(
        "the uvx cache is keyed by the revision",
        argv[1:3] == ["--cache-dir", f"/worker-state/cache/uvx/tauceti-review/{REVIEW_REF}"],
    )
    check("the source is the pinned revision", f"git+https://github.com/{REVIEW}@{REVIEW_REF}" in argv)
    check("the pinned source is what --from names", argv[argv.index("--from") + 1].endswith(f"@{REVIEW_REF}"))
    check("the engine command and its args follow", argv[-4:] == ["tauceti-review", "42", "--store", "/s"])
    if REVIEW_PYTHON:
        check("the interpreter constraint is passed", argv[argv.index("--python") + 1] == REVIEW_PYTHON)
        check("macOS needs 3.13 for os.waitid", sys.platform != "darwin" or REVIEW_PYTHON == ">=3.13")
    else:
        check("no interpreter constraint where none is needed", "--python" not in argv)
    py = re_mod.engine_argv(state, "-c", "pass", command="python")
    check("another command from the same environment", py[-3:] == ["python", "-c", "pass"])

    os.environ["TAUCETI_REVIEW_ENGINE_DIR"] = "/eng"
    local = re_mod.engine_argv(state, "42", "--sync-only")
    check(
        "a local checkout runs on this interpreter",
        local == [sys.executable, "/eng/runner/cli.py", "42", "--sync-only"],
    )
    ok, note = re_mod.engine_selfcheck(state)
    check("a local checkout is not checked", ok and "/eng" in note)
    os.environ.pop("TAUCETI_REVIEW_ENGINE_DIR")

    # 2) the self-check.
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="3.13.12\n", stderr="")

    orig = re_mod.subprocess.run
    re_mod.subprocess.run = fake_run
    try:
        ok, note = re_mod.engine_selfcheck(state)
        check("a passing check is ok", ok)
        check("a passing check names the pin and interpreter", REVIEW_REF[:12] in note and "3.13.12" in note)
        check(
            "the check runs python from the pinned engine", calls and calls[0][calls[0].index("--from") + 2] == "python"
        )
        check(
            "the check exercises the engine's git helper", "runner.pr_diff" in calls[0][-1] and "init" in calls[0][-1]
        )

        def failing_run(argv, **kw):
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="Traceback (most recent call last):\n  ...\nAttributeError: no waitid\n"
            )

        re_mod.subprocess.run = failing_run
        ok, note = re_mod.engine_selfcheck(state)
        check("a failing check is not ok", not ok)
        check("a failing check reports the last line", note == "AttributeError: no waitid")

        def missing_run(argv, **kw):
            raise FileNotFoundError("uvx")

        re_mod.subprocess.run = missing_run
        ok, note = re_mod.engine_selfcheck(state)
        check("a launch error is reported, not raised", not ok and "uvx" in note)
    finally:
        re_mod.subprocess.run = orig

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
