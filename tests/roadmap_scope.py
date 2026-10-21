"""Roadmap list validation and concrete authoring selection, without network or model work."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tauceti_worker import cli, config, work_units
from tauceti_worker.survey import Candidate
from tauceti_worker.survey_facts import in_scope
from tauceti_worker.tui import _expansion_rows
from tauceti_worker.worker_manager import WorkersError, WorkerSpec

AREAS = ["GraphConnectivityAndFlows", "StandardDistributions"]
SCOPE = ",".join(AREAS)


class RoadmapScopeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"TAUCETI_ROADMAP_ONLY": "", "TAUCETI_ROADMAP_SKIP": ""}))

    def test_scope_keeps_metadata_fallbacks_and_shared_maintenance(self):
        for area in AREAS:
            self.assertTrue(in_scope(["roadmap/" + area], roadmap=SCOPE, skip=AREAS))
            self.assertTrue(in_scope([], [area], "roadmap/work", SCOPE))
        self.assertTrue(in_scope([], roadmap=SCOPE))
        self.assertTrue(in_scope(["roadmap/Unknown"], roadmap=SCOPE))
        self.assertFalse(in_scope(["roadmap/Topology"], roadmap=SCOPE))

    def test_validation_normalizes_and_checks_every_area(self):
        os.environ["TAUCETI_ROADMAP_ONLY"] = f" {AREAS[0]}, {AREAS[1]}, {AREAS[0]}, "
        with patch.object(config, "roadmap_areas", return_value=AREAS):
            config.validate_roadmap_scope(object())
            self.assertEqual(config.roadmap_only(), SCOPE)
            for value in ("Missing", f"{SCOPE},Missing", AREAS[0].lower()):
                os.environ["TAUCETI_ROADMAP_ONLY"] = value
                with self.assertRaisesRegex(config.Die, "unknown roadmap area.*--roadmap-only"):
                    config.validate_roadmap_scope(object())
            os.environ["TAUCETI_ROADMAP_ONLY"] = SCOPE
            os.environ["TAUCETI_ROADMAP_SKIP"] = "Missing"
            with self.assertRaisesRegex(config.Die, "unknown roadmap area.*--roadmap-skip"):
                config.validate_roadmap_scope(object())

    def test_unavailable_inventory_is_a_distinct_error(self):
        os.environ["TAUCETI_ROADMAP_ONLY"] = SCOPE
        with patch.object(config, "roadmap_areas", return_value=[]):
            with self.assertRaisesRegex(config.Die, "cannot validate roadmap areas: could not load"):
                config.validate_roadmap_scope(object())
        with patch.object(config, "roadmap_areas", side_effect=OSError("lookup failed")):
            with self.assertRaisesRegex(config.Die, "cannot validate roadmap areas:.*lookup failed"):
                config.validate_roadmap_scope(object())

    def test_special_modes_need_no_inventory(self):
        with patch.object(config, "roadmap_areas") as areas:
            for value in ("", "any", "auto"):
                os.environ["TAUCETI_ROADMAP_ONLY"] = value
                config.validate_roadmap_scope(object())
            os.environ.pop("TAUCETI_ROADMAP_ONLY")
            config.validate_roadmap_scope(object())
            areas.assert_not_called()
            os.environ["TAUCETI_ROADMAP_ONLY"] = "auto," + SCOPE
            with self.assertRaisesRegex(config.Die, "must be used alone"):
                config.validate_roadmap_scope(object())

    def test_invalid_cli_and_inherited_scope_stop_before_work(self):
        for command, flags in (("work", ["--loop", "--roadmap-only", "Missing"]), ("_round", [])):
            os.environ["TAUCETI_ROADMAP_ONLY"] = "Missing"
            args = cli.build_parser().parse_args([command, *flags])
            with (
                patch.object(config, "roadmap_areas", return_value=AREAS),
                patch.object(cli, "cmd_loop") as loop,
                patch.object(cli, "auto_assign_wid") as reserve,
                patch("tauceti_worker.github_outbox.drain") as drain,
            ):
                with self.assertRaisesRegex(config.Die, "unknown roadmap area.*Missing"):
                    cli.cmd_work(args, only=[], agent="claude", one_round=command == "_round")
                loop.assert_not_called()
                reserve.assert_not_called()
                drain.assert_not_called()

    def test_authoring_chooses_one_area_and_checks_its_claims(self):
        os.environ["TAUCETI_ROADMAP_SKIP"] = SCOPE + ",Topology"
        os.environ["TAUCETI_RESPECT_CLAIMS"] = "true"
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(state=Path(tmp), checkout=Path(tmp), logdir=Path(tmp), wid="test")
            worker = SimpleNamespace(cfg=cfg, gh=object())
            opts = SimpleNamespace(agent_name="Claude Code", work_model="claude")
            for area in AREAS:
                with (
                    patch.object(work_units.random, "choice", return_value=area) as choose,
                    patch.object(work_units, "administrative_hold_avoid_list", return_value="none") as holds,
                    patch.object(work_units, "claimed_avoid_list", return_value="none") as claims,
                    patch.object(work_units, "fetch_ref", return_value=True),
                    patch.object(work_units, "stage_rubrics", return_value=None),
                    patch.object(work_units, "ensure_fork", return_value="author/TauCeti"),
                    patch.object(work_units, "prepare_checkout", return_value=True) as checkout,
                    patch.object(work_units, "run_agent_host", return_value=0) as agent,
                ):
                    self.assertEqual(work_units.do_roadmap(worker, None, Candidate(0, "", SCOPE), opts, False), 0)
                    choose.assert_called_once_with(tuple(AREAS))
                    holds.assert_called_once_with(worker.gh, area)
                    claims.assert_called_once_with(worker.gh, area)
                    checkout.assert_called_once_with(cfg, work="roadmap", roadmap=area)
                    prompt = agent.call_args.args[1]
                    self.assertIn(f"Start with `{area}`", prompt)
                    self.assertNotIn(SCOPE, prompt)
                    self.assertIn("Never target `Topology`", prompt)

    def test_dashboard_marks_all_selected_areas(self):
        survey = SimpleNamespace(roadmap_only=SCOPE, roadmap_skip=AREAS)
        rows = _expansion_rows(survey, "roadmap", {}, AREAS + ["Topology"])
        self.assertEqual([label.strip() for label, tag in rows if tag == "← only"], ["└ " + a for a in AREAS])

    def test_managed_source_requires_one_area(self):
        with self.assertRaisesRegex(WorkersError, "single concrete roadmap_only"):
            WorkerSpec.from_dict({"id": "test", "only": ["roadmap"], "roadmap_only": SCOPE, "source": "/tmp"}, 0)


if __name__ == "__main__":
    unittest.main()
