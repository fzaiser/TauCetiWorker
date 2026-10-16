"""Automatic rounds tend PRs across roadmaps before selecting an authoring area."""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker.config import NoProgress
from tauceti_worker.github_state import SurveyRows
from tauceti_worker.review_state import ReviewState
from tauceti_worker.survey_facts import CONTRACT, in_scope, summarize
from tauceti_worker.work_units import RoundOpts, run_round

survey = importlib.import_module("tauceti_worker.survey")
work = importlib.import_module("tauceti_worker.work_units")
head = "a" * 40
areas = ["Geometry", "PDE", "Topology", "Algebra", "Analysis", "Groups", "NumberTheory", "Logic"]
raw = [
    {
        "number": n,
        "author": {"login": "author"},
        "headRefOid": head,
        "headRefName": f"roadmap/work-{n}",
        "headRepositoryOwner": {"login": "author"},
        "labels": [{"name": "roadmap/" + area}],
        "mergeable": "CONFLICTING" if n == 1 else "MERGEABLE",
        "statusCheckRollup": [{"context": "build", "state": "FAILURE" if n == 2 else "SUCCESS"}],
    }
    for n, area in enumerate(areas, 1)
]


def issues(number):
    if number == 4:
        return []
    blocking = number == 3
    meta = {
        "head_sha": head,
        "runs": [{"provider": "codex", "verdict": "request_changes" if blocking else "approve"}],
        "states": {"reuse": "blocking_request" if blocking else "green"},
    }
    return [{"body": "<!--tauceti-scoreboard--> <!--tauceti-meta:v1 " + json.dumps(meta) + "-->"}]


def run(*, gateway, only, rows=raw, env=None, numbers=(), spent=False, dry_run=False):
    selected = []
    env = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")} | (env or {})
    env["TAUCETI_MAX_OPEN_PRS"] = "8"

    def read_survey(repo, roadmap, skip, *, numbers, stages):
        if not gateway:
            return False, None
        scoped = [
            p
            for p in rows
            if (
                p["number"] in numbers
                if numbers
                else in_scope([label["name"] for label in p["labels"]], roadmap=roadmap, skip=skip)
            )
        ]
        compact = [
            {
                "contract": CONTRACT,
                "pr": p,
                "review": {**summarize(issues(p["number"]), []), "issue_complete": True, "review_complete": True},
            }
            for p in scoped
        ]
        count = sum(in_scope([label["name"] for label in p["labels"]], skip=skip) for p in rows)
        return True, SurveyRows(compact, {"author": count})

    def dispatch(stage, worker, sv, candidate, opts):
        selected.append((stage, candidate.pr, candidate.reason, sv.n_mine_open, sv.roadmap_only))
        return 0

    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.dict(os.environ, env, clear=True),
        patch.object(survey, "me", return_value="author"),
        patch.object(survey, "can_push", return_value=False),
        patch("tauceti_worker.github_state.read_survey", side_effect=read_survey),
        patch("tauceti_worker.github_transport.event"),
        patch.object(work, "mirror_creds"),
        patch.object(work, "roadmap_areas", return_value=["Unrelated"]) as area_list,
        patch.object(work, "dispatch", side_effect=dispatch),
    ):
        cfg = SimpleNamespace(wid="test", state=Path(tmp), sbcache=Path(tmp) / "sb", store_dir=Path(tmp) / "review")
        gh = SimpleNamespace(
            repo="org/repo", open_prs=lambda: rows, issue_comments=issues, review_comments=lambda _: []
        )
        counters = SimpleNamespace(read=lambda _: 100 if spent else 0)
        worker = SimpleNamespace(cfg=cfg, gh=gh, rs=ReviewState(cfg, gh), counters=counters)
        opts = RoundOpts(only=only, agent="codex", work_model="auto", sandbox_host=True, dry_run=dry_run, prs=numbers)
        try:
            assert run_round(worker, opts) == 0
        except NoProgress as error:
            assert spent and "backpressure" in str(error), error
        area_list.assert_not_called()
    return selected


for gateway in (False, True):
    for stage, number in (("rebase", 1), ("fix-ci", 2), ("fix", 3), ("review", 4)):
        picked = run(gateway=gateway, only=[stage, "roadmap"])
        assert picked[0][:2] == (stage, number), (gateway, picked)
        assert picked[0][3:] == (8, "auto"), "maintenance must remain available at the global authoring limit"
    picked = run(gateway=gateway, only=["rebase", "fix-ci", "fix", "review", "roadmap"])
    assert picked[0][:2] == ("rebase", 1), "automatic mode must preserve maintenance priority"
    picked = run(gateway=gateway, only=["fix", "roadmap"], env={"TAUCETI_ROADMAP_ONLY": "Topology"})
    assert picked[0][:2] == ("fix", 3) and picked[0][3:] == (1, "Topology")
    picked = run(
        gateway=gateway,
        only=["rebase", "fix-ci", "fix", "roadmap"],
        env={"TAUCETI_ROADMAP_SKIP": "Geometry,PDE"},
    )
    assert picked[0][:2] == ("fix", 3) and picked[0][3] == 6, "skipped roadmaps must stay excluded"
    picked = run(
        gateway=gateway,
        only=["fix", "review", "roadmap"],
        env={"TAUCETI_ROADMAP_ONLY": "Topology", "TAUCETI_ROADMAP_SKIP": "Algebra"},
        numbers=(4,),
    )
    assert picked[0][:2] == ("review", 4), "explicit PR targets must still override roadmap filters"
    picked = run(gateway=gateway, only=["fix", "roadmap"], dry_run=True)
    assert picked[0][:2] == ("fix", 3), "dry runs and live rounds must select the same work"
    assert not run(gateway=gateway, only=["fix", "roadmap"], spent=True), "exhausted attempts must stay suppressed"
    picked = run(gateway=gateway, only=["fix", "review", "roadmap"], rows=raw[4:])
    assert picked == [("roadmap", 0, "auto", 4, "auto")], "choose an area only after reaching authoring"

print(
    "PASS: automatic maintenance spans roadmaps on direct and compact reads; filters, budgets and authoring remain scoped"
)
