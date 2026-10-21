"""Whole-library header checks use one Lean process and preserve every failure."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class LintStyleTests(unittest.TestCase):
    def test_large_inventory_checks_every_file_once_and_retains_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "TauCeti" / ("LongModuleDirectory" * 6)
            directory.mkdir(parents=True)
            files = []
            for number in range(20000):
                source = directory / f"Module{number:05}.lean"
                source.touch()
                files.append(str(source.relative_to(root)))
            (root / ".lake").mkdir()
            tools = root / "bin"
            tools.mkdir()
            lake = tools / "lake"
            lake.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "headers = args[:3] == ['env', 'lean', '--run']\n"
                "if headers:\n"
                "    assert len(args) == 5\n"
                "    paths = Path(args[4]).read_text().splitlines()\n"
                "with open(os.environ['TEST_LINT_LOG'], 'a') as log:\n"
                "    log.write(json.dumps(paths if headers else args) + '\\n')\n"
                "if headers:\n"
                "    sys.exit(int(os.environ.get('TEST_LINT_FAIL') in paths))\n"
                "imports = Path(os.environ['LEAN_SRC_PATH'].split(os.pathsep)[0]) / 'TauCetiLint/All.lean'\n"
                "assert len(imports.read_text().splitlines()) == 20001\n"
                "sys.exit(int(os.environ.get('TEST_STYLE_FAIL', '0')))\n"
            )
            lake.chmod(0o755)
            log = root / "calls.jsonl"
            env = {
                **os.environ,
                "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                "PYTHONPATH": str(REPO),
                "TEST_LINT_LOG": str(log),
            }
            self.assertGreater(sum(len(path) + 1 for path in files), 2 * 1024**2)
            for failure, style_failure in (("", False), (files[150], False), ("", True)):
                with self.subTest(header_failure=bool(failure), style_failure=style_failure):
                    log.unlink(missing_ok=True)
                    result = subprocess.run(
                        [sys.executable, "-m", "tauceti_worker.style_lint", "--quiet"],
                        cwd=root,
                        env={**env, "TEST_LINT_FAIL": failure, "TEST_STYLE_FAIL": str(int(style_failure))},
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, int(bool(failure) or style_failure), result.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0], files)
                    self.assertEqual(calls[-1], ["exe", "lint-style", "--quiet", "TauCetiLint.All", "TauCeti"])


if __name__ == "__main__":
    unittest.main()
