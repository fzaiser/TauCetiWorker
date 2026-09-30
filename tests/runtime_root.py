#!/usr/bin/env python3
"""Runtime dirs of an installed worker live in the user's state directory, not beside the package.

state/, logs/ and checkouts/ used to sit next to the code in both layouts. In a source checkout that
is right (the Docker image mounts them there). In a wheel it put every worker's counters,
review-failure records and leases under site-packages, where `uv tool upgrade` or a reinstall deleted
them, and a clone-based `./tauceti` saw a different state than the installed `tauceti`.

Pinned here:

  1. A source checkout keeps the root beside the code; a wheel uses $TAUCETI_RUNTIME_ROOT, else
     $XDG_STATE_HOME/tauceti, else the platform's per-user state directory under the LOGIN home.
  2. A child's environment carries the resolved root, so an isolated $HOME cannot move it.
  3. Migration renames the three dirs once, leaves an existing target alone, and survives a failed
     rename by leaving the old directory in place.

Exit 0 = all assertions hold; 1 = a mismatch.
"""

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker.config as config
import tauceti_worker.paths as paths

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(f"[{'OK ' if cond else 'BAD'}] {name}")


def main():
    pkg, checkout, home = Path("/site/tauceti_worker"), Path("/src/TauCetiWorker"), Path("/home/me")
    root = paths._runtime_root

    # 1) where the root goes.
    check(
        "a source checkout keeps its runtime dirs beside the code", root({}, checkout, pkg, "linux", home) == checkout
    )
    check(
        "an installed wheel on Linux uses ~/.local/state/tauceti",
        root({}, pkg, pkg, "linux", home) == home / ".local" / "state" / "tauceti",
    )
    check(
        "an installed wheel on macOS uses Application Support",
        root({}, pkg, pkg, "darwin", home) == home / "Library" / "Application Support" / "tauceti",
    )
    check(
        "$XDG_STATE_HOME wins over the platform default",
        root({"XDG_STATE_HOME": "/xdg"}, pkg, pkg, "darwin", home) == Path("/xdg/tauceti"),
    )
    check(
        "$TAUCETI_RUNTIME_ROOT wins over everything, even in a checkout",
        root({"TAUCETI_RUNTIME_ROOT": "/elsewhere", "XDG_STATE_HOME": "/xdg"}, checkout, pkg, "linux", home)
        == Path("/elsewhere"),
    )
    check(
        "the live root is one of those",
        paths.RUNTIME_ROOT
        == root(dict(__import__("os").environ), paths.HERE, paths._pkg, sys.platform, paths._login_home()),
    )

    # 2) children resolve the same root.
    env = paths.self_env({"PATH": "/usr/bin"})
    check("self_env exports the resolved root", env.get("TAUCETI_RUNTIME_ROOT") == str(paths.RUNTIME_ROOT))
    kept = paths.self_env({"TAUCETI_RUNTIME_ROOT": "/pinned"})
    check("self_env keeps an explicit root", kept["TAUCETI_RUNTIME_ROOT"] == "/pinned")

    # 3) migration.
    with tempfile.TemporaryDirectory() as d:
        old, new = Path(d) / "old", Path(d) / "new"
        (old / "state" / "w1").mkdir(parents=True)
        (old / "state" / "w1" / "counter").write_text("3")
        (old / "logs" / "w1").mkdir(parents=True)
        (new / "checkouts").mkdir(parents=True)  # pre-existing target: left alone
        (old / "checkouts" / "w1").mkdir(parents=True)
        config.migrate_runtime_dirs(old, new)
        check("state moves with its contents", (new / "state" / "w1" / "counter").read_text() == "3")
        check("logs move", (new / "logs" / "w1").is_dir())
        check("the old state dir is gone", not (old / "state").exists())
        check("an existing target is not touched", not (new / "checkouts" / "w1").exists())
        check("its source is left in place", (old / "checkouts" / "w1").is_dir())
        config.migrate_runtime_dirs(old, new)
        check("a second migration is a no-op", (new / "state" / "w1" / "counter").read_text() == "3")
        config.migrate_runtime_dirs(new, new)
        check("the same root on both sides is a no-op", (new / "state" / "w1").is_dir())

        blocked = Path(d) / "blocked"
        blocked.write_text("a file where the root should be")
        config.migrate_runtime_dirs(new, blocked / "root")  # mkdir fails: ENOTDIR
        check("a failed rename leaves the old dirs in place", (new / "state" / "w1" / "counter").read_text() == "3")

    print("FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
