"""tauceti_worker.review_engine — how the host runs the pinned tauceti-review engine, and the check
that it can run here at all."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import one_line
from .constants import REVIEW, REVIEW_PYTHON, REVIEW_REF

# Runs the engine's own git helper on the interpreter uvx resolved for it: no model, no credential, a
# temp dir only. The interpreter is the point — the helper needs os.waitid, which CPython does not
# provide on every platform/version pair the engine's metadata admits. An upstream rename of the
# helper degrades this to an import check rather than a false alarm.
_SELFCHECK = """\
import os, sys, tempfile
import runner.pr_diff as pr_diff
run = getattr(pr_diff, "_git", None)
if run is not None:
    with tempfile.TemporaryDirectory() as d:
        run(d, ["init", "-q", "--bare"], dict(os.environ), 60)
print(sys.version.split()[0])
"""


def engine_source() -> str:
    return f"git+https://github.com/{REVIEW}@{REVIEW_REF}"


def engine_argv(state: Path, *args: str, command: str = "tauceti-review") -> list[str]:
    """argv running `command` from the pinned engine revision with `args`.

    $TAUCETI_REVIEW_ENGINE_DIR names a local checkout to run instead (on this worker's interpreter);
    otherwise uvx fetches the pinned revision. The uvx environment is cached per revision, as for
    tauceti-progress: uv keys its shared tool environment by package name and version, which the
    engine never bumps, so a pin bump could otherwise keep running the previous checkout.
    """
    local = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")
    if local:
        if command == "tauceti-review":
            return [sys.executable, str(Path(local) / "runner" / "cli.py"), *args]
        return [sys.executable, *args]
    cache = state / "cache" / "uvx" / "tauceti-review" / REVIEW_REF
    argv = ["uvx", "--cache-dir", str(cache)]
    if REVIEW_PYTHON:
        argv += ["--python", REVIEW_PYTHON]
    return [*argv, "--from", engine_source(), command, *args]


def engine_selfcheck(state: Path, timeout: int = 600) -> tuple[bool, str]:
    """Whether the pinned engine can run on this host: (ok, detail).

    Cheap once the engine is cached (well under a second) and free of side effects beyond that cache,
    so a round can afford it before every survey. The first run fetches the engine and, if uvx has no
    interpreter satisfying REVIEW_PYTHON, downloads one; `timeout` covers that."""
    local = os.environ.get("TAUCETI_REVIEW_ENGINE_DIR")
    if local:
        return True, f"local checkout {local} (not checked)"
    argv = engine_argv(state, "-c", _SELFCHECK, command="python")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, one_line(str(e))
    if p.returncode == 0:
        return True, f"{REVIEW}@{REVIEW_REF[:12]} on Python {p.stdout.strip().splitlines()[-1]}"
    lines = [ln for ln in (p.stderr or p.stdout or "").splitlines() if ln.strip()]
    return False, one_line(lines[-1] if lines else f"exit {p.returncode}")
