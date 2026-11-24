from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .evaluator import evaluate_plan, normalize_action


def id_sort_key(value: Any) -> tuple[tuple[int, Any], ...]:
    text = str(value)
    parts = re.findall(r"\d+|\D+", text)
    key: list[tuple[int, Any]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


@dataclass
class CandidateInfo:
    index: int
    raw: dict[str, Any]
    action: dict[str, Any]
    eligible_mask: int
    eligible_work_sets: list[str]
    rejection: dict[str, Any]
    binding_index: int = -1
    region_index: int = 0


def evaluate_candidates(case: Any) -> list[CandidateInfo]:
    work_set_ids = case.work_set_ids
    index_by_ws = {wsid: i for i, wsid in enumerate(work_set_ids)}
    infos: list[CandidateInfo] = []

    for i, cand in enumerate(case.candidates):
        cid = str(cand.get("id") or f"C{i + 1:02d}")
        action = normalize_action(cand, case, {"window": case.raw.get("window")}, i)
        probe_plan = {
            "id": f"__probe_{cid}",
            "window": case.raw.get("window"),
            "actions": [{"candidate_id": cid}],
        }
        report = evaluate_plan(case, probe_plan)
        action_report = report["actions"][0] if report.get("actions") else {"violations": [{"code": "NO_ACTION_REPORT"}]}

        eligible: list[str] = []
        assignment_rows = report.get("assignments", [])
        for row in assignment_rows:
            wsid = row.get("work_set_id")
            if wsid in index_by_ws and row.get("approved") and not action_report.get("violations"):
                eligible.append(wsid)

        # If the candidate omitted work_sets, evaluate_plan assigned all case work sets.
        mask = 0
        for wsid in eligible:
            mask |= 1 << index_by_ws[wsid]

        rejection: dict[str, Any] = {}
        if mask == 0:
            codes: list[str] = []
            for violation in action_report.get("violations", []):
                code = violation.get("code")
                if code and code not in codes:
                    codes.append(code)
            for row in assignment_rows:
                for evidence in row.get("failure_evidence", []):
                    code = evidence.get("code")
                    if code and code not in codes:
                        codes.append(code)
            rejection = {
                "candidate_id": cid,
                "codes": codes,
                "action_violations": action_report.get("violations", [])[:5],
            }

        infos.append(
            CandidateInfo(
                index=i,
                raw=cand,
                action=action,
                eligible_mask=mask,
                eligible_work_sets=eligible,
                rejection=rejection,
            )
        )
    return infos


def _build_bindings(infos: list[CandidateInfo]) -> tuple[int, dict[str, int]]:
    """Union actions on one node for the same explicit window or overlapping interval."""
    n = len(infos)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        left = infos[i].action
        for j in range(i + 1, n):
            right = infos[j].action
            if not left.get("node") or left.get("node") != right.get("node"):
                continue
            same_window = left.get("window_id") == right.get("window_id")
            from .timeutil import intervals_overlap

            overlap = intervals_overlap(left.get("interval"), right.get("interval"))
            if same_window or overlap:
                union(i, j)

    roots = sorted({find(i) for i in range(n)}, key=lambda r: str(infos[r].action.get("node") or "") + f":{r}")
    root_to_binding = {root: idx for idx, root in enumerate(roots)}
    binding_descriptions: dict[str, int] = {}
    for i, info in enumerate(infos):
        root = find(i)
        info.binding_index = root_to_binding[root]
        key = f"{info.action.get('node') or '__no_node__'}::{info.action.get('window_id')}"
        binding_descriptions[key] = info.binding_index

    regions = sorted({str(info.action.get("region")) for info in infos if info.action.get("region")})
    region_to_index = {region: i + 1 for i, region in enumerate(regions)}
    for info in infos:
        info.region_index = region_to_index.get(str(info.action.get("region")), 0)
    return len(roots), region_to_index


def _objective_key(cost: int, count: int, ids: list[str]) -> tuple[Any, ...]:
    ordered = sorted(ids, key=id_sort_key)
    return (cost, count, tuple(id_sort_key(x) for x in ordered))


def recommend(case: Any, *, max_actions: int = 18) -> dict[str, Any]:
    work_set_ids = case.work_set_ids
    full_mask = (1 << len(work_set_ids)) - 1
    infos = evaluate_candidates(case)
    binding_count, _ = _build_bindings(infos)

    usable = [info for info in infos if info.eligible_mask]
    usable.sort(key=lambda info: id_sort_key(info.action["action_id"]))

    zero_bindings = tuple([0] * binding_count)
    # state -> (objective key, selected ids)
    dp: dict[tuple[int, tuple[int, ...]], tuple[tuple[Any, ...], tuple[str, ...]]] = {
        (0, zero_bindings): ((0, 0, ()), ())
    }

    for info in usable:
        action = info.action
        cid = action["action_id"]
        downtime = int(action.get("downtime_minutes", 0))
        next_dp = dict(dp)  # skipping this candidate keeps every existing state
        b = info.binding_index
        r = info.region_index

        for (mask, bindings), (old_key, selected_ids) in dp.items():
            count = old_key[1]
            if count + 1 > max_actions:
                continue
            if bindings[b] not in (0, r):
                continue
            new_bindings_list = list(bindings)
            new_bindings_list[b] = r
            new_bindings = tuple(new_bindings_list)
            new_mask = mask | info.eligible_mask
            new_ids = tuple([*selected_ids, cid])  # usable is sorted by natural ID
            key = _objective_key(old_key[0] + downtime, count + 1, list(new_ids))
            state = (new_mask, new_bindings)
            existing = next_dp.get(state)
            if existing is None or key < existing[0]:
                next_dp[state] = (key, new_ids)
        dp = next_dp

    best: tuple[tuple[Any, ...], tuple[str, ...]] | None = None
    for (mask, _bindings), value in dp.items():
        if mask != full_mask:
            continue
        key, ids = value
        if key[1] > max_actions:
            continue
        if best is None or key < best[0]:
            best = value

    feasibility_rows = [
        {
            "candidate_id": info.action["action_id"],
            "eligible_work_sets": info.eligible_work_sets,
            "eligible_mask": info.eligible_mask,
            "downtime_minutes": info.action.get("downtime_minutes", 0),
            "node": info.action.get("node"),
            "region": info.action.get("region"),
            "window_id": info.action.get("window_id"),
            "rejection": info.rejection or None,
        }
        for info in infos
    ]

    if best is None:
        blocking: list[dict[str, Any]] = []
        for i, wsid in enumerate(work_set_ids):
            bit = 1 << i
            if not any(info.eligible_mask & bit for info in infos):
                related = [
                    {
                        "candidate_id": info.action["action_id"],
                        "codes": info.rejection.get("codes", []),
                        "action_violations": info.rejection.get("action_violations", [])[:3],
                    }
                    for info in infos
                    if info.rejection
                ]
                code_counts = sorted({code for row in related for code in row.get("codes", [])})
                blocking.append(
                    {
                        "code": "NO_FEASIBLE_CANDIDATE_FOR_WORK_SET",
                        "work_set_id": wsid,
                        "candidate_rejection_codes": code_counts,
                        "candidate_evidence": related[:8],
                    }
                )
        if not blocking:
            blocking.append(
                {
                    "code": "MAX_ACTIONS_OR_NODE_CONSTRAINTS",
                    "message": "all work sets have candidates, but no subset satisfies action cap and node-region constraints",
                    "max_actions": max_actions,
                }
            )
        return {
            "schema": "vaultdrill.recommendation.v1",
            "case_id": case.case_id,
            "feasible": False,
            "work_set_count": len(work_set_ids),
            "max_actions": max_actions,
            "candidate_count": len(infos),
            "minimal_failure_evidence": blocking,
            "candidate_feasibility": feasibility_rows,
        }

    key, selected_ids_tuple = best
    selected_ids = list(selected_ids_tuple)
    selected_infos = {info.action["action_id"]: info for info in infos}

    assignments: dict[str, str] = {}
    action_ws: dict[str, list[str]] = {cid: [] for cid in selected_ids}
    for i, wsid in enumerate(work_set_ids):
        bit = 1 << i
        choices = [cid for cid in selected_ids if selected_infos[cid].eligible_mask & bit]
        # Stable ascending-ID assignment. This does not change selected action cost/count.
        choices.sort(key=id_sort_key)
        chosen = choices[0]
        assignments[wsid] = chosen
        action_ws[chosen].append(wsid)

    plan_actions: list[dict[str, Any]] = []
    for cid in sorted(selected_ids, key=id_sort_key):
        info = selected_infos[cid]
        action = info.action
        plan_actions.append(
            {
                "id": cid,
                "candidate_id": action.get("candidate_id") or cid,
                "snapshot": action.get("snapshot"),
                "node": action.get("node"),
                "region": action.get("region"),
                "recovery_at": action["at"].isoformat() if action.get("at") else None,
                "window_id": action.get("window_id"),
                "downtime_minutes": action.get("downtime_minutes", 0),
                "read_groups": action.get("read_groups", []),
                "work_sets": sorted(action_ws[cid], key=id_sort_key),
            }
        )

    plan = {
        "schema": "vaultdrill.plan.v1",
        "id": f"recommended-{case.case_id}",
        "case_id": case.case_id,
        "window": case.raw.get("window"),
        "actions": plan_actions,
    }
    evaluation = evaluate_plan(case, plan)

    return {
        "schema": "vaultdrill.recommendation.v1",
        "case_id": case.case_id,
        "feasible": True,
        "work_set_count": len(work_set_ids),
        "max_actions": max_actions,
        "candidate_count": len(infos),
        "selected_action_ids": sorted(selected_ids, key=id_sort_key),
        "objective": {
            "downtime_minutes": key[0],
            "action_count": key[1],
            "ascending_id_tie_break": selected_ids,
        },
        "work_set_assignments": assignments,
        "recommended_actions": plan_actions,
        "actions": plan_actions,
        "plan": plan,
        "evaluation_summary": evaluation["summary"],
        "evaluation": evaluation,
        "candidate_feasibility": feasibility_rows,
    }


def exhaustive_recommend(case: Any, *, max_actions: int = 18) -> dict[str, Any]:
    """Independent subset enumeration oracle for small candidate sets."""
    work_set_ids = case.work_set_ids
    full_mask = (1 << len(work_set_ids)) - 1
    infos = evaluate_candidates(case)
    n = len(infos)
    best_ids: list[str] | None = None
    best_key: tuple[Any, ...] | None = None

    for subset in range(1 << n):
        ids: list[str] = []
        coverage = 0
        cost = 0
        region_by_binding: dict[tuple[str, str], str] = {}
        valid = True
        count = subset.bit_count()
        if count > max_actions:
            continue
        for i, info in enumerate(infos):
            if not (subset >> i) & 1:
                continue
            if not info.eligible_mask:
                valid = False
                break
            action = info.action
            # Independent node-window check using explicit IDs and intervals.
            binding = (str(action.get("node")), str(action.get("window_id")))
            region = str(action.get("region"))
            if binding in region_by_binding and region_by_binding[binding] != region:
                valid = False
                break
            region_by_binding[binding] = region
            ids.append(action["action_id"])
            coverage |= info.eligible_mask
            cost += int(action.get("downtime_minutes", 0))
        if not valid or coverage != full_mask:
            continue

        # The quick binding map catches equal window IDs; also catch interval overlaps
        # across differently named windows.
        selected_actions = [infos[i].action for i in range(n) if (subset >> i) & 1]
        from .timeutil import intervals_overlap

        for i, left in enumerate(selected_actions):
            for right in selected_actions[i + 1 :]:
                if left.get("node") != right.get("node") or left.get("region") == right.get("region"):
                    continue
                if left.get("window_id") == right.get("window_id") or intervals_overlap(
                    left.get("interval"), right.get("interval")
                ):
                    valid = False
                    break
            if not valid:
                break
        if not valid:
            continue

        key = _objective_key(cost, len(ids), ids)
        if best_key is None or key < best_key:
            best_key = key
            best_ids = sorted(ids, key=id_sort_key)

    return {
        "feasible": best_ids is not None,
        "selected_action_ids": best_ids or [],
        "objective": None if best_key is None else {
            "downtime_minutes": best_key[0],
            "action_count": best_key[1],
            "ascending_id_tie_break": best_ids or [],
        },
    }
