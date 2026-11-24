from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vaultdrill.evaluator import evaluate_plan  # noqa: E402
from vaultdrill.loader import Case, WorkSet, load_case, load_plan  # noqa: E402
from vaultdrill.recommend import exhaustive_recommend, recommend  # noqa: E402
from tools.exhaustive_check import make_case  # noqa: E402

EVAL_DIR = ROOT / "examples" / "evaluate"
REC_CASE = ROOT / "examples" / "recommend" / "case.json"


class EvaluateSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = load_case(EVAL_DIR / "case.json")
        cls.plan = load_plan(EVAL_DIR / "plan.json")
        cls.report = evaluate_plan(cls.case, cls.plan)

    def test_rename_keeps_stable_id_and_shows_chain(self) -> None:
        ws = next(w for w in self.report["work_sets"] if w["work_set_id"] == "ws-good-rename-readd")
        assignment = ws["assignments"][0]
        rename = next(o for o in assignment["objects"] if o["object_id"] == "obj-rename")
        self.assertEqual(rename["status"], "live")
        self.assertEqual(rename["source_snapshot"], "inc-002-rename")
        self.assertEqual(
            rename["snapshot_chain"][-1],
            "full-001",
        )
        self.assertIn("inc-005-latest", rename["snapshot_chain"])
        self.assertEqual(rename["digest"], "sha256:rename-v2")
        self.assertIn("grp-finance-readers", rename["allowed_groups"])

    def test_parent_delete_then_readd_uses_new_entry(self) -> None:
        ws = next(w for w in self.report["work_sets"] if w["work_set_id"] == "ws-good-rename-readd")
        readd = next(o for o in ws["assignments"][0]["objects"] if o["object_id"] == "obj-readd")
        self.assertEqual(readd["source_snapshot"], "inc-004-readd-same-stable-id")
        self.assertEqual(readd["digest"], "sha256:readd-v2")
        self.assertIn("inc-003-parent-table-delete", readd["snapshot_chain"])
        trace = {row["snapshot"]: row for row in readd["table_trace"]}
        self.assertEqual(trace["inc-003-parent-table-delete"]["table_state"], "delete_marker")

    def test_delete_marker_masks_inherited_copy_with_minimal_evidence(self) -> None:
        ws = next(w for w in self.report["work_sets"] if w["work_set_id"] == "ws-delete-marker")
        self.assertFalse(ws["recoverable"])
        obj = ws["assignments"][0]["objects"][0]
        self.assertEqual(obj["status"], "deleted")
        evidence = ws["assignments"][0]["failure_evidence"][0]
        self.assertEqual(evidence["code"], "OBJECT_DELETED_BY_MARKER")
        self.assertEqual(evidence["delete_marker_snapshot"], "inc-005-latest")
        self.assertEqual(evidence["masked_inherited_copy_snapshot"], "full-001")

    def test_individual_recoverable_but_no_common_window(self) -> None:
        ws = next(w for w in self.report["work_sets"] if w["work_set_id"] == "ws-no-common-window")
        self.assertFalse(ws["recoverable"])
        for obj in ws["assignments"][0]["objects"]:
            self.assertTrue(obj["recoverable"], f"{obj['object_id']} should be individually recoverable")
        evidence = next(
            e for e in ws["assignments"][0]["failure_evidence"] if e["code"] == "EMPTY_PERMISSION_INTERSECTION"
        )
        owners = {w["owner"] for w in evidence["minimal_evidence"]["windows"]}
        self.assertEqual(owners, {"obj-window-a", "obj-window-b"})

    def test_wrong_group_and_sealed_leak_and_node_conflict(self) -> None:
        codes = {f["code"] for f in self.report["failures"]}
        self.assertIn("GROUP_NOT_AUTHORIZED", codes)
        self.assertIn("SEALED_OBJECT_BROUGHT_OUT", codes)
        self.assertIn("NODE_REGION_CONFLICT", codes)
        self.assertFalse(self.report["summary"]["approved"])
        self.assertEqual(self.report["summary"]["approved_work_sets"], 1)

    def test_cli_prints_utf8_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "vaultdrill.py"), "evaluate",
             str(EVAL_DIR / "case.json"), str(EVAL_DIR / "plan.json")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.encode("utf-8").decode("utf-8"))
        self.assertEqual(payload["schema"], "vaultdrill.evaluation.v1")
        self.assertIn("snapshot_chain", proc.stdout)


class RecommendSampleTests(unittest.TestCase):
    def test_sample_matches_exhaustive_oracle(self) -> None:
        case = load_case(REC_CASE)
        result = recommend(case, max_actions=18)
        brute = exhaustive_recommend(case, max_actions=18)
        self.assertTrue(result["feasible"])
        self.assertLessEqual(len(result["selected_action_ids"]), 18)
        self.assertEqual(len(case.work_sets), 14)
        self.assertEqual(result["selected_action_ids"], brute["selected_action_ids"])
        self.assertEqual(
            result["objective"]["downtime_minutes"],
            brute["objective"]["downtime_minutes"],
        )
        evaluation = result["evaluation_summary"]
        self.assertTrue(evaluation["approved"])
        self.assertEqual(evaluation["violation_or_failure_count"], 0)
        self.assertEqual(evaluation["work_set_count"], 14)

    def test_dp_matches_bruteforce_on_deterministic_small_cases(self) -> None:
        for seed in range(1, 81):
            case = make_case(seed)
            cap = (seed % len(case.candidates)) + 1
            fast = recommend(case, max_actions=cap)
            brute = exhaustive_recommend(case, max_actions=cap)
            self.assertEqual(fast.get("feasible"), brute["feasible"], seed)
            if fast.get("feasible"):
                self.assertEqual(fast["selected_action_ids"], brute["selected_action_ids"], seed)
                self.assertEqual(
                    fast["objective"]["downtime_minutes"],
                    brute["objective"]["downtime_minutes"],
                    seed,
                )
                self.assertEqual(
                    fast["objective"]["action_count"],
                    brute["objective"]["action_count"],
                    seed,
                )


class OptimizerRuleTests(unittest.TestCase):
    def _case(self, candidates, n_ws=2):
        ws_ids = [f"WS{i}" for i in range(1, n_ws + 1)]
        object_ids = [f"obj-{i}" for i in range(n_ws)]
        snapshot = {
            "id": "full",
            "parent": None,
            "kind": "full",
            "objects": {oid: {"id": oid, "digest": oid} for oid in object_ids},
            "delete_markers": {},
            "regions": [],
            "recovery_window": None,
            "access_rule_version": "rv",
        }
        return Case(
            path=ROOT / "memory-case.json",
            root=ROOT,
            case_id="optimizer-rules",
            snapshots={"full": snapshot},
            snapshot_dir=ROOT,
            rules={
                "versions": {
                    "rv": {
                        "default_object_policy": {
                            "allowed_groups": ["g"],
                            "sealed": False,
                            "permission_window": {
                                "start": "2026-12-08T00:00:00Z",
                                "end": "2026-12-09T00:00:00Z",
                            },
                        }
                    }
                }
            },
            work_sets={
                wsid: WorkSet(id=wsid, object_ids=[object_ids[i]], mandatory_ids=[object_ids[i]])
                for i, wsid in enumerate(ws_ids)
            },
            nodes={},
            candidates=candidates,
            raw={"window": {"id": "w", "start": "2026-12-08T02:00:00Z"}, "default_read_groups": ["g"]},
        )

    def test_equal_cost_and_count_prefers_ascending_id_sequence(self) -> None:
        def action(cid, ws):
            return {
                "id": cid,
                "snapshot": "full",
                "node": "n1",
                "region": "cn-north",
                "window_id": "w",
                "downtime_minutes": 1,
                "work_sets": ws,
            }

        case = self._case(
            [
                action("A1", ["WS1", "WS2"]),
                action("A2", ["WS3"]),
                action("A3", ["WS1", "WS2", "WS4"]),
                action("A4", ["WS3", "WS4"]),
            ],
            n_ws=4,
        )
        result = recommend(case, max_actions=18)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["selected_action_ids"], ["A1", "A4"])
        brute = exhaustive_recommend(case, max_actions=18)
        self.assertEqual(result["selected_action_ids"], brute["selected_action_ids"])

    def test_action_cap_makes_recommendation_infeasible(self) -> None:
        candidates = [
            {
                "id": f"A{i}",
                "snapshot": "full",
                "node": "n1",
                "region": "cn-north",
                "window_id": "w",
                "downtime_minutes": 1,
                "work_sets": [f"WS{i}"],
            }
            for i in range(1, 4)
        ]
        case = self._case(candidates, n_ws=3)
        result = recommend(case, max_actions=2)
        self.assertFalse(result["feasible"])
        self.assertTrue(result["minimal_failure_evidence"])


class CliErrorTests(unittest.TestCase):
    def test_invalid_json_returns_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-I", str(ROOT / "vaultdrill.py"), "recommend", str(bad)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "INVALID_JSON")


if __name__ == "__main__":
    unittest.main(verbosity=2)
