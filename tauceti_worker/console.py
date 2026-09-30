"""tauceti_worker.console — what a person at a terminal sees of the worker's log.

The log itself does not change: the per-worker file, and the console of a managed worker (which the
manager keeps as its durable log), get every line in the plain `date time tauceti: message` form.
Only an interactive, unmanaged terminal gets this rendering: a clock instead of a full timestamp, no
prefix, paths relative to the runtime root, detail lines hidden unless --verbose, and a status line
that the loop rewrites while a round runs.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from .paths import RUNTIME_ROOT

_DIM, _RED, _YELLOW, _GREEN, _RESET = "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m"
_CLEAR = "\r\033[K"  # back to column 0 and wipe the line: removes a status line before a log line
_STYLE = {"debug": _DIM, "warn": _YELLOW, "error": _RED, "ok": _GREEN}
_human: bool | None = None
_status_shown = False


def human() -> bool:
    """An interactive terminal that nothing else reads: stderr is a TTY, no manager owns it, and the
    plain form was not asked for."""
    global _human
    if _human is None:
        _human = bool(
            sys.stderr.isatty() and not os.environ.get("TAUCETI_MANAGED") and not os.environ.get("TAUCETI_PLAIN_LOG")
        )
    return _human


def verbose() -> bool:
    return bool(os.environ.get("TAUCETI_VERBOSE"))


def short_path(text: str) -> str:
    """Paths under the runtime root become relative (`logs/worker1/…`); the home dir becomes `~`."""
    text = text.replace(str(RUNTIME_ROOT) + "/", "")
    home = str(Path.home())
    return text.replace(home + "/", "~/") if home not in ("", "/") else text


def emit(msg: str, level: str) -> None:
    """One log line on the terminal. Detail lines only show with --verbose."""
    if level == "debug" and not verbose():
        return
    style = _STYLE.get(level, "")
    body = f"{style}{short_path(msg)}{_RESET}" if style else short_path(msg)
    print(f"{_CLEAR}{_DIM}{time.strftime('%H:%M:%S')}{_RESET}  {body}", file=sys.stderr, flush=True)


def status(text: str) -> None:
    """Rewrite the transient status line in place (no newline)."""
    global _status_shown
    width = shutil.get_terminal_size((100, 20)).columns
    text = short_path(text)
    if len(text) >= width:
        text = text[: max(0, width - 2)] + "…"
    print(f"{_CLEAR}{_DIM}{text}{_RESET}", end="", file=sys.stderr, flush=True)
    _status_shown = True


def clear_status() -> None:
    global _status_shown
    if _status_shown:
        print(_CLEAR, end="", file=sys.stderr, flush=True)
        _status_shown = False


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def short_target(target: object) -> str:
    """The round's target without its URL: `PR #9943  https://…` becomes `PR #9943`."""
    return str(target or "").split("  ", 1)[0].strip()
