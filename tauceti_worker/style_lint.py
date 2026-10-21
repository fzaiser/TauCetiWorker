"""Run shared source style checks without passing the file inventory through argv."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from tauceti_tools.project import active
from tauceti_tools.templates import ASSETS, materialize

from .constants import LEAN_ROOT


def header_driver(source: Path, directory: Path, paths: list[str]) -> tuple[Path, Path]:
    checker = source.read_text()
    entry = "unsafe def main (args : List String) : IO UInt32 := do"
    if checker.count(entry) != 1:
        raise ValueError("lint-style: unsupported shared header checker entry point")
    # Keep the shared checker intact; only its entry point reads a manifest so
    # a large inventory needs neither an oversized argv nor repeated Lean startup.
    driver = directory / "HeaderStyle.lean"
    driver.write_text(
        checker.replace(entry, entry.replace("main", "auditHeaders"))
        + "\nunsafe def main (args : List String) : IO UInt32 := do\n"
        + "  let [manifest] := args | return 1\n"
        + '  let paths := (← IO.FS.readFile manifest).splitOn "\\n"\n'
        + '  auditHeaders (paths.filter (· != ""))\n'
    )
    manifest = directory / "header-files.txt"
    manifest.write_text("".join(f"{path}\n" for path in paths))
    return driver, manifest


def main(args=None):
    root = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="lint-style-", dir=root / ".lake") as tmp:
        directory = Path(tmp)
        shared = ASSETS / "scripts"
        if project := active():
            shared = directory / "scripts"
            materialize(project, shared, policy_dir=root / "scripts")
        files, modules = directory / "files", directory / "modules"
        discovered = subprocess.run(
            [
                "bash",
                "-euo",
                "pipefail",
                "-c",
                '. "$1"; tauceti_source_modules "$2" "$3"',
                "source-modules",
                str(shared / "source-modules.sh"),
                str(files),
                str(modules),
            ]
        )
        if discovered.returncode:
            return 1
        paths = [os.fsdecode(path) for path in files.read_bytes().split(b"\0") if path]
        names = modules.read_text().splitlines()
        if not paths or len(paths) != len(names):
            print("lint-style: incomplete source inventory; refusing to run", file=sys.stderr)
            return 1
        # Mathlib's style linter checks imports, while the library root is empty.
        module = LEAN_ROOT + "Lint.All"
        imports = directory / (module.replace(".", "/") + ".lean")
        imports.parent.mkdir(parents=True, exist_ok=True)
        imports.write_text("".join(f"import {name}\n" for name in [LEAN_ROOT, *names]))
        driver, manifest = header_driver(shared / "HeaderStyle.lean", directory, paths)
        result = subprocess.run(["lake", "env", "lean", "--run", str(driver), str(manifest)])
        failed = result.returncode != 0
        env = dict(
            os.environ,
            LEAN_SRC_PATH=str(directory)
            + (os.pathsep + os.environ["LEAN_SRC_PATH"] if os.environ.get("LEAN_SRC_PATH") else ""),
        )
        result = subprocess.run(["lake", "exe", "lint-style", *(args or []), module, LEAN_ROOT], env=env)
        return int(failed or result.returncode != 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
