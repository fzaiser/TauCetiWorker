#!/usr/bin/env python3
"""Default Claude authoring probes Opus 5.5 safely and falls back to a model this host serves.

Claude Code answers a model it cannot serve with an API-style error. One cause is a build older than
the model ("version 2.1.280 or newer is required"); the account may well have the model, but this
host cannot use it until `claude update`. The probe treats that like a missing entitlement: confirm it
twice, take the first served model of the chain, cache the answer against the build and config dir so
that an update alone re-probes, and never downgrade on an answer that confirmed nothing.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

OPUS55 = "claude-opus-5-5"
OPUS5 = "claude-opus-5"
SONNET55 = "claude-sonnet-5-5"
TOO_OLD_MESSAGE = (
    "API Error: 400 Claude Code 2.1.278 does not support this model; version 2.1.280 or newer is "
    "required. Run 'claude update', or update the Claude desktop app, then try again."
)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(f"[{'OK ' if ok else 'XX '}] {name}: got={got!r} want={want!r}")
    fails += not ok


def result(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "ok"}
    return SimpleNamespace(returncode=0, stdout=json.dumps({**base, **fields}) + "\n", stderr="")


OK = result()
TOO_OLD = result(
    is_error=True, api_error_status=400, api_error_code="claude_code_version_too_old", result=TOO_OLD_MESSAGE
)
NOT_FOUND = result(
    is_error=True, api_error_status=404, api_error_code="not_found_error", result="API Error: 404 model: claude-x"
)
OVERLOADED = result(is_error=True, api_error_status=529, result="API Error: 529 Overloaded")
CONTEXT = result(is_error=True, api_error_status=400, result="API Error: 400 prompt is too long")
CRASH = SimpleNamespace(returncode=1, stdout="", stderr="TypeError: boom\n")
RAW_FALSE_POSITIVE = SimpleNamespace(returncode=1, stdout="error: unknown model\n", stderr="")


def run(sequence, *, repeat=False, explicit=False, versions=("2.1.278",)):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = SimpleNamespace(home=root / "home", quota_cache=root / "cache", state=root / "state")
        profile = tc.AuthoringProfile(
            provider="claude",
            model=OPUS55,
            effort="high",
            model_source="--author-model" if explicit else "repository default",
            effort_source="repository default",
            fallback_model=None if explicit else f"{OPUS5},{SONNET55}",
        )
        calls, logged = [], []
        outcomes = list(sequence)
        versions = list(versions)
        saved_run, saved_log = tc.agents.subprocess.run, tc.agents.log
        saved_env = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CONFIG_DIR")}
        os.environ["ANTHROPIC_API_KEY"] = "must-not-reach-subscription-probe"
        os.environ["CLAUDECODE"] = "1"
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "worker" / ".claude")

        def fake_run(argv, **kwargs):
            if argv[1:] == ["--version"]:
                return SimpleNamespace(returncode=0, stdout=f"{versions[0]} (Claude Code)\n", stderr="")
            calls.append((argv, kwargs))
            return outcomes.pop(0)

        tc.agents.subprocess.run = fake_run
        tc.agents.log = lambda msg, **_k: logged.append(msg)
        try:
            try:
                selected = tc.resolve_model_access(cfg, profile)
                error = None
            except tc.NoProgress as exc:
                selected, error = None, exc
            again = None
            if repeat and selected is not None:
                if len(versions) > 1:
                    versions.pop(0)
                again = tc.resolve_model_access(cfg, profile)
        finally:
            tc.agents.subprocess.run, tc.agents.log = saved_run, saved_log
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return selected, again, error, calls, outcomes, logged


def probed(calls):
    return [argv[argv.index("--model") + 1] for argv, _ in calls]


selected, again, error, calls, remaining, _ = run([OK], repeat=True)
check("a served Opus 5.5 probe keeps Opus 5.5", (selected.model, error), (OPUS55, None))
check("the served answer is cached", (again.model, len(calls), len(remaining)), (OPUS55, 1, 0))
argv, kwargs = calls[0]
check(
    "probe is one -p turn on the requested model",
    (argv[0], argv[1], argv[argv.index("--model") + 1]),
    ("claude", "-p", OPUS55),
)
check("probe prompt is trivial, not an authoring prompt", argv[2], "Reply with exactly OK. Do not use tools.")
check(
    "probe reads a JSON result and keeps no session", ("json" in argv, "--no-session-persistence" in argv), (True, True)
)
check("probe runs outside every checkout", str(kwargs.get("cwd", "")).startswith(tempfile.gettempdir()), True)
env = kwargs.get("env", {})
check(
    "probe strips API-key billing and the nested-session marker",
    ("ANTHROPIC_API_KEY" in env, "CLAUDECODE" in env),
    (False, False),
)
check(
    "probe keeps the worker's config dir",
    env.get("CLAUDE_CONFIG_DIR"),
    os.environ.get("CLAUDE_CONFIG_DIR", env.get("CLAUDE_CONFIG_DIR")),
)

selected, again, error, calls, remaining, logged = run([TOO_OLD, TOO_OLD, OK], repeat=True)
check("a build too old for Opus 5.5, confirmed twice, authors on Opus 5", (selected.model, error), (OPUS5, None))
check("Opus 5.5 is confirmed twice, then Opus 5 probed", probed(calls), [OPUS55, OPUS55, OPUS5])
check("the fallback is cached without a fourth request", (again.model, len(calls), len(remaining)), (OPUS5, 3, 0))
check(
    "the log says why, with Claude Code's own way out", any("claude update" in m and OPUS5 in m for m in logged), True
)

selected, again, error, calls, remaining, _ = run(
    [TOO_OLD, TOO_OLD, OK, OK], repeat=True, versions=("2.1.278", "2.1.286")
)
check(
    "an updated Claude Code re-probes and gets Opus 5.5 back",
    (selected.model, again.model, probed(calls)),
    (OPUS5, OPUS55, [OPUS55, OPUS55, OPUS5, OPUS55]),
)

selected, _, error, calls, remaining, _ = run([NOT_FOUND, NOT_FOUND, NOT_FOUND, OK])
check(
    "a model the account lacks falls through the chain in order",
    (selected.model, probed(calls)),
    (SONNET55, [OPUS55, OPUS55, OPUS5, SONNET55]),
)

selected, _, error, calls, _, _ = run([TOO_OLD, TOO_OLD, TOO_OLD, TOO_OLD])
check("a host serving none of them does not launch", selected, None)
check("...and names every model tried", all(m in str(error) for m in (OPUS55, OPUS5, SONNET55)), True)
check("...as a failure every authoring round would repeat", getattr(error, "scope", None), "machine")
check(
    "...naming the ways out",
    ("TAUCETI_AUTHORING_CLAUDE_MODEL" in str(error), "--agent codex" in str(error)),
    (True, True),
)

selected, _, error, calls, _, _ = run([TOO_OLD, OK])
check("a served reconfirmation keeps Opus 5.5", (selected.model, len(calls), error), (OPUS55, 2, None))
selected, _, error, calls, _, _ = run([TOO_OLD, TOO_OLD, OVERLOADED])
check("an unconfirmed Opus 5 probe failure does not downgrade or launch", (selected, error is not None), (None, True))

for name, outcome in (
    ("overloaded 529", OVERLOADED),
    ("context 400", CONTEXT),
    ("crashed CLI", CRASH),
    ("raw transcript false positive", RAW_FALSE_POSITIVE),
):
    selected, _, error, calls, _, _ = run([outcome])
    check(f"{name} does not downgrade", (selected, len(calls), error is not None), (None, 1, True))

selected, _, error, calls, _, _ = run([], explicit=True)
check("explicit model bypasses probe and fallback", (selected.model, len(calls), error), (OPUS55, 0, None))

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)
