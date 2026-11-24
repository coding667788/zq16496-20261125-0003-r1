#!/usr/bin/env python3
"""Small deterministic exhaustive cross-check for vaultdrill's optimizer.

It generates tiny set-cover/node-region cases, compares the DP recommendation
with every candidate subset, and also checks the checked-in 18-candidate sample.
Standard library only.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vaultdrill.loader import load_case  # noqa: E402
from vaultdrill.loader import Case, WorkSet  # noqa: E402
from vaultdrill.recommend import exhaustive_recommend, recommend  # noqa: E402


def make_case(seed: int) -> Case:
    rng = random.Random(seed)
    n_ws = rng.randint(3, 6)
    n_candidates = rng.randint(4, 9)
    ws_ids = [f"WS{i:02d}" for i in range(1, n_ws + 1)]
    object_ids = [f"obj-{i:02d}" for i in range(n_ws)]

    work_sets = {
        wsid: WorkSet(id=wsid, object_ids=[object_ids[i]], mandatory_ids=[object_ids[i]])
        for i, wsid in enumerate(ws_ids)
    }
    snapshot = {
        "id": "full-tiny",
        "parent": None,
        "kind": "full",
        "objects": {oid: {"id": oid, "digest": f"sha256:{oid}"} for oid in object_ids},
        "delete_markers": {},
        "regions": [],
        "recovery_window": None,
        "access_rule_version": "rv-tiny",
    }
    candidates = []
    full_mask = (1 << n_ws) - 1
    for i in range(n_candidates):
        while True:
            mask = rng.randint(1, full_mask)
            if mask:
                break
        covered = [ws_ids[b] for b in range(n_ws) if mask & (1 << b)]
        candidates.append(
            {
                "id": f"C{i:02d}",
                "snapshot": "full-tiny",
                "node": f"node-{rng.randint(1, 2)}",
                "region": rng.choice(["cn-north", "cn-south"]),
                "window_id": rng.choice(["w0", "w1"]),
                "downtime_minutes": rng.randint(1, 20),
                "work_sets": covered,
            }
        )

    raw = {
        "id": f"random-{seed}",
        "window": {"id": "w0", "start": "2026-12-08T02:30:00Z"},
        "default_read_groups": ["grp-drill-readers"],
    }
    rules = {
        "versions": {
            "rv-tiny": {
                "default_object_policy": {
                    "allowed_groups": ["grp-drill-readers"],
                    "sealed": False,
                    "permission_window": {
                        "start": "2026-12-08T00:00:00Z",
                        "end": "2026-12-09T00:00:00Z",
                    },
                }
            }
        }
    }
    return Case(
        path=ROOT / "tools" / f"random-{seed}.json",
        root=ROOT,
        case_id=f"random-{seed}",
        snapshots={"full-tiny": snapshot},
        snapshot_dir=ROOT / "snapshots",
        rules=rules,
        work_sets=work_sets,
        nodes={},
        candidates=candidates,
        raw=raw,
    )


def check_random(iterations: int = 300) -> int:
    for seed in range(1, iterations + 1):
        case = make_case(seed)
        cap = random.Random(10_000 + seed).randint(1, max(3, len(case.candidates)))
        fast = recommend(case, max_actions=cap)
        brute = exhaustive_recommend(case, max_actions=cap)
        if fast.get("feasible") != brute["feasible"]:
            raise AssertionError(
                f"seed {seed}: feasibility mismatch fast={fast.get('feasible')} brute={brute['feasible']} cap={cap}"
            )
        if fast.get("feasible"):
            if fast["selected_action_ids"] != brute["selected_action_ids"]:
                raise AssertionError(
                    f"seed {seed}: selected mismatch fast={fast['selected_action_ids']} "
                    f"brute={brute['selected_action_ids']} cap={cap}"
                )
            fast_obj = (
                fast["objective"]["downtime_minutes"],
                fast["objective"]["action_count"],
                fast["selected_action_ids"],
            )
            brute_obj = (
                brute["objective"]["downtime_minutes"],
                brute["objective"]["action_count"],
                brute["selected_action_ids"],
            )
            if fast_obj != brute_obj:
                raise AssertionError(f"seed {seed}: objective mismatch {fast_obj} != {brute_obj}")
    print(f"random exhaustive cross-check: {iterations} cases passed")
    return iterations


def check_sample() -> None:
    case = load_case(ROOT / "examples" / "recommend" / "case.json")
    fast = recommend(case, max_actions=18)
    brute = exhaustive_recommend(case, max_actions=18)
    if not fast.get("feasible"):
        raise AssertionError("checked-in recommendation sample is infeasible")
    if fast["selected_action_ids"] != brute["selected_action_ids"]:
        raise AssertionError(f"sample mismatch: {fast['selected_action_ids']} vs {brute['selected_action_ids']}")
    if fast["objective"]["downtime_minutes"] != brute["objective"]["downtime_minutes"]:
        raise AssertionError("sample downtime mismatch")
    print(
        "sample exhaustive cross-check: selected "
        + ",".join(fast["selected_action_ids"])
        + f" ({fast['objective']['downtime_minutes']} min, {fast['objective']['action_count']} actions)"
    )


def main() -> int:
    count = check_random()
    check_sample()
    print(f"vaultdrill exhaustive checks passed ({count} random cases + sample)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
