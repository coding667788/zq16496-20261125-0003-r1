from __future__ import annotations

from typing import Any

from .chain import build_chain, check_snapshot_access, materialize_view, resolve_object
from .policy import allowed_groups, is_still_sealed, object_policy, policy_window
from .timeutil import (
    intersect_windows,
    intervals_overlap,
    minimal_empty_intersection,
    moment_from_action,
    action_interval,
    parse_window,
)


def candidate_map(case: Any) -> dict[str, dict[str, Any]]:
    return {str(c.get("id")): c for c in case.candidates if c.get("id") is not None}


def find_candidate(case: Any, ref: Any) -> dict[str, Any] | None:
    if ref in (None, ""):
        return None
    return candidate_map(case).get(str(ref))


def normalize_action(
    raw: dict[str, Any], case: Any, plan: dict[str, Any], index: int
) -> dict[str, Any]:
    candidates = candidate_map(case)
    ref = raw.get("candidate_id") or raw.get("candidate") or raw.get("ref") or raw.get("candidate_ref")
    cand = find_candidate(case, ref)
    if cand is None and raw.get("id") in candidates and ref is None:
        cand = candidates[str(raw["id"])]

    merged: dict[str, Any] = {}
    if cand:
        merged.update(cand)
    # Plan fields override candidate fields, but an explicit null should not erase.
    for key, value in raw.items():
        if value is not None:
            merged[key] = value

    action_id = str(raw.get("id") or (cand or {}).get("id") or ref or f"action-{index + 1:02d}")
    candidate_id = str((cand or {}).get("id") or ref or "") if (cand or ref) else None

    wsid_value = merged.get("work_sets", merged.get("work_set", merged.get("assign", [])))
    if isinstance(wsid_value, str):
        wsid_value = [wsid_value]
    work_sets = [str(x) for x in (wsid_value or [])]
    if not work_sets:
        work_sets = list(case.work_set_ids)

    groups = merged.get("read_groups", merged.get("groups", merged.get("requested_groups")))
    if groups is None:
        groups = plan.get("read_groups", plan.get("groups", plan.get("requested_groups")))
    if groups is None:
        groups = case.raw.get("default_read_groups", [])
    if isinstance(groups, str):
        groups = [groups]
    read_groups = [str(x) for x in groups]

    window_id = merged.get("window_id") or merged.get("recovery_window_id")
    window_value = merged.get("window", merged.get("recovery_window"))
    window_obj = window_value if isinstance(window_value, dict) else {}
    if not window_id and isinstance(window_value, str):
        window_id = window_value
    if not window_id and window_obj:
        window_id = window_obj.get("id") or window_obj.get("window_id")
    if not window_id:
        plan_window = plan.get("window", plan.get("recovery_window"))
        window_id = plan_window.get("id") if isinstance(plan_window, dict) else None
    if not window_id:
        case_window = case.raw.get("window", case.raw.get("recovery_window"))
        window_id = case_window.get("id") if isinstance(case_window, dict) else None
    window_id = str(window_id or "__default_window__")

    region = merged.get("region") or merged.get("accessible_region") or merged.get("selected_region")
    node = merged.get("node") or merged.get("node_id") or merged.get("recovery_node")
    at = moment_from_action(merged, plan)
    interval = action_interval(merged)
    downtime = merged.get("downtime_minutes", merged.get("downtime", merged.get("minutes", 0)))
    try:
        downtime_int = int(downtime)
    except (TypeError, ValueError):
        downtime_int = 0

    return {
        "action_id": action_id,
        "candidate_id": candidate_id,
        "id": action_id,
        "snapshot": str(merged["snapshot"]) if merged.get("snapshot") not in (None, "") else merged.get("snapshot_id"),
        "node": str(node) if node not in (None, "") else None,
        "region": str(region) if region not in (None, "") else None,
        "at": at,
        "window_id": window_id,
        "interval": interval,
        "work_sets": work_sets,
        "read_groups": read_groups,
        "downtime_minutes": max(0, downtime_int),
        "raw": merged,
    }


def _governing_policies(case: Any, action: dict[str, Any], resolved: dict[str, Any]) -> list[dict[str, Any]]:
    source_version = resolved.get("access_rule_version")
    selected_id = action.get("snapshot")
    selected_snap = case.snapshots.get(selected_id) if selected_id else None
    selected_version = selected_snap.get("access_rule_version") if selected_snap else None
    strategy = str(case.raw.get("rule_version_strategy", "source"))

    versions: list[str | None]
    if strategy == "selected":
        versions = [selected_version or source_version]
    elif strategy == "intersection":
        versions = list(dict.fromkeys([source_version, selected_version]))
    else:
        versions = [source_version or selected_version]

    policies: list[dict[str, Any]] = []
    for version in versions:
        policy, effective_version, evidence = object_policy(case, resolved["object_id"], version)
        policy["_effective_version"] = effective_version
        policy["_policy_evidence"] = evidence
        policies.append(policy)
    return policies


def evaluate_object(case: Any, action: dict[str, Any], object_id: str) -> dict[str, Any]:
    resolved = resolve_object(case, action.get("snapshot"), object_id)
    report: dict[str, Any] = {
        "object_id": object_id,
        "recoverable": bool(resolved["recoverable"]),
        "status": resolved["status"],
        "name": resolved.get("name"),
        "digest": resolved.get("digest"),
        "source_snapshot": resolved.get("source_snapshot"),
        "selected_snapshot": resolved.get("selected_snapshot"),
        "full_snapshot": resolved.get("full_snapshot"),
        "snapshot_chain": resolved.get("snapshot_chain"),
        "access_rule_version": resolved.get("access_rule_version"),
        "source_access_rule_version": resolved.get("access_rule_version"),
        "selected_access_rule_version": (
            case.snapshots.get(action.get("snapshot"), {}).get("access_rule_version")
            if action.get("snapshot") in case.snapshots
            else None
        ),
        "allowed_groups_by_version": {},
        "delete_marker_snapshot": resolved.get("delete_marker_snapshot"),
        "masked_copy_snapshot": resolved.get("masked_copy_snapshot"),
        "masked_copy_name": resolved.get("masked_copy_name"),
        "masked_copy_digest": resolved.get("masked_copy_digest"),
        "allowed_groups": [],
        "requested_groups": action.get("read_groups", []),
        "allowed_requested_groups": [],
        "denied_groups": [],
        "sealed": False,
        "permission_windows": [],
        "failure_evidence": list(resolved.get("failure_evidence", [])),
        "table_trace": resolved.get("table_trace", []),
    }

    if not resolved["recoverable"]:
        return report

    policies = _governing_policies(case, action, resolved)
    group_sets = [set(allowed_groups(p)) for p in policies]
    if group_sets:
        final_groups = set.intersection(*group_sets) if len(group_sets) > 1 else group_sets[0]
    else:
        final_groups = set()
    report["allowed_groups"] = sorted(final_groups)
    report["rule_versions_considered"] = [p.get("_effective_version") for p in policies]
    report["allowed_groups_by_version"] = {
        str(p.get("_effective_version")): sorted(allowed_groups(p))
        for p in policies
        if p.get("_effective_version") is not None
    }
    selected_version = report["selected_access_rule_version"]
    if selected_version:
        selected_policy, _, _ = object_policy(case, object_id, selected_version)
        report["selected_rule_allowed_groups"] = sorted(allowed_groups(selected_policy))
    for policy in policies:
        for evidence in policy.get("_policy_evidence", []):
            if evidence.get("code") == "UNKNOWN_RULE_VERSION":
                report["failure_evidence"].append(
                    {"code": "UNKNOWN_RULE_VERSION", "object_id": object_id, **evidence}
                )

    sealed_reasons: list[str] = []
    for policy in policies:
        sealed, reason = is_still_sealed(policy, action.get("at"))
        if sealed:
            report["sealed"] = True
            sealed_reasons.append(f"{policy.get('_effective_version')}: {reason}")
    if sealed_reasons:
        report["failure_evidence"].append(
            {
                "code": "OBJECT_STILL_SEALED",
                "object_id": object_id,
                "reasons": sealed_reasons,
            }
        )

    windows = []
    for policy in policies:
        window = policy_window(policy, object_id)
        if window is not None:
            windows.append(window)
    report["permission_windows"] = [w.to_json() for w in windows]

    requested = set(action.get("read_groups", []))
    report["allowed_requested_groups"] = sorted(requested & final_groups)
    report["denied_groups"] = sorted(requested - final_groups)
    if report["denied_groups"]:
        report["failure_evidence"].append(
            {
                "code": "GROUP_NOT_AUTHORIZED",
                "object_id": object_id,
                "requested_groups": sorted(requested),
                "allowed_groups": sorted(final_groups),
                "denied_groups": report["denied_groups"],
                "access_rule_versions": report["rule_versions_considered"],
            }
        )

    return report


def evaluate_action(case: Any, action: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    action_id = action["action_id"]

    if not action.get("snapshot"):
        violations.append(
            {"code": "MISSING_SNAPSHOT", "action_id": action_id, "message": "action requires a snapshot"}
        )
    if not action.get("region"):
        violations.append(
            {"code": "MISSING_REGION", "action_id": action_id, "message": "action requires a recovery region"}
        )

    node_id = action.get("node")
    region = action.get("region")
    if case.nodes and node_id and node_id not in case.nodes:
        violations.append(
            {
                "code": "UNKNOWN_NODE",
                "action_id": action_id,
                "node": node_id,
                "message": "recovery node is not declared in case.nodes",
            }
        )
    elif node_id in case.nodes:
        allowed_regions = case.nodes[node_id].get("regions") or []
        if region and allowed_regions and region not in allowed_regions:
            violations.append(
                {
                    "code": "NODE_REGION_NOT_ALLOWED",
                    "action_id": action_id,
                    "node": node_id,
                    "requested_region": region,
                    "allowed_regions": allowed_regions,
                }
            )

    view = materialize_view(case, action.get("snapshot"))
    chain = view["chain"]
    for err in chain.get("errors", []):
        violations.append({"action_id": action_id, **err})

    at = action.get("at")
    if at is None and action.get("snapshot"):
        # Missing time is a violation only when a snapshot window needs checking.
        # check_snapshot_access emits MISSING_RECOVERY_TIME where applicable.
        pass

    snapshot_access: list[dict[str, Any]] = []
    for snap in chain.get("snapshots", []):
        failures = check_snapshot_access(snap, region, at)
        if failures:
            snapshot_access.extend(failures)
            for failure in failures:
                violations.append({"action_id": action_id, **failure})

    brought_objects: list[dict[str, Any]] = []
    sealed_objects: list[dict[str, Any]] = []
    for object_id in sorted(view["live"]):
        info = view["live"][object_id]
        source_version = info.get("access_rule_version")
        pseudo_resolved = {"object_id": object_id, "access_rule_version": source_version}
        policies = _governing_policies(case, action, pseudo_resolved)
        sealed = False
        reasons: list[str] = []
        versions: list[str | None] = []
        for policy in policies:
            versions.append(policy.get("_effective_version"))
            policy_sealed, reason = is_still_sealed(policy, at)
            if policy_sealed:
                sealed = True
                reasons.append(f"{policy.get('_effective_version')}: {reason}")
        item = {
            "object_id": object_id,
            "source_snapshot": info.get("snapshot"),
            "access_rule_version": source_version,
            "rule_versions_considered": versions,
            "sealed": sealed,
        }
        brought_objects.append(item)
        if sealed:
            sealed_item = {**item, "reason": "; ".join(re for re in reasons if re)}
            sealed_objects.append(sealed_item)
            violations.append(
                {
                    "code": "SEALED_OBJECT_BROUGHT_OUT",
                    "action_id": action_id,
                    "snapshot": action.get("snapshot"),
                    "object_id": object_id,
                    "source_snapshot": info.get("snapshot"),
                    "access_rule_version": source_version,
                    "rule_versions_considered": versions,
                    "reason": sealed_item["reason"],
                    "message": "recovery materialization would bring out an object that is still sealed",
                }
            )

    return {
        "action_id": action_id,
        "candidate_id": action.get("candidate_id"),
        "snapshot": action.get("snapshot"),
        "node": node_id,
        "region": region,
        "recovery_at": at.isoformat() if at else None,
        "window_id": action.get("window_id"),
        "downtime_minutes": action.get("downtime_minutes", 0),
        "work_sets": action.get("work_sets", []),
        "read_groups": action.get("read_groups", []),
        "snapshot_chain": chain.get("snapshot_chain", []),
        "full_snapshot": chain.get("full_snapshot"),
        "brought_object_count": len(brought_objects),
        "brought_objects": brought_objects,
        "sealed_objects": sealed_objects,
        "snapshot_access_failures": snapshot_access,
        "violations": violations,
        "usable": not violations,
    }


def evaluate_assignment(case: Any, action: dict[str, Any], action_report: dict[str, Any], wsid: str) -> dict[str, Any]:
    ws = case.work_sets.get(wsid)
    result: dict[str, Any] = {
        "work_set_id": wsid,
        "action_id": action["action_id"],
        "candidate_id": action.get("candidate_id"),
        "recoverable": False,
        "approved": False,
        "object_count": 0 if ws is None else len(ws.object_ids),
        "objects": [],
        "permission_intersection": None,
        "failure_evidence": [],
    }
    if ws is None:
        result["failure_evidence"].append(
            {"code": "UNKNOWN_WORK_SET", "work_set_id": wsid, "message": "work set is not declared in case"}
        )
        return result

    # Work-set-specific requested groups override action/global only if action has none?
    # normalize_action already applied global/case; preserve explicit action. If action raw
    # omitted groups and ws defines them, use ws here.
    if not action.get("read_groups") and ws.read_groups:
        action = {**action, "read_groups": ws.read_groups}

    object_reports = [evaluate_object(case, action, oid) for oid in ws.object_ids]
    result["objects"] = object_reports

    failures: list[dict[str, Any]] = []
    for obj in object_reports:
        failures.extend(obj.get("failure_evidence", []))

    windows = []
    for obj in object_reports:
        # Reconstruct Window objects because reports store JSON.
        for raw_window in obj.get("permission_windows", []):
            win = parse_window(raw_window, owner=raw_window.get("owner"), label="permission")
            if win is not None:
                windows.append(win)

    witness = minimal_empty_intersection(windows)
    intersection = intersect_windows(windows)
    if witness is not None:
        failures.append(
            {
                "code": "EMPTY_PERMISSION_INTERSECTION",
                "work_set_id": wsid,
                "message": "objects are individually recoverable, but no common permission window exists",
                "minimal_evidence": witness.to_json(),
            }
        )
    elif intersection is not None:
        result["permission_intersection"] = intersection.to_json()
        at = action.get("at")
        if at is None:
            failures.append(
                {
                    "code": "MISSING_RECOVERY_TIME",
                    "work_set_id": wsid,
                    "message": "recovery instant is required to use the permission-window intersection",
                    "permission_intersection": intersection.to_json(),
                }
            )
        elif not intersection.contains(at):
            failures.append(
                {
                    "code": "RECOVERY_INSTANT_OUTSIDE_PERMISSION_WINDOW",
                    "work_set_id": wsid,
                    "recovery_at": at.isoformat(),
                    "permission_intersection": intersection.to_json(),
                }
            )

    # Technical recoverability includes chain/snapshot/window failures, but not
    # authorization denials. Approval below is stricter and rejects any evidence.
    technical_codes = {
        "OBJECT_DELETED_BY_MARKER",
        "OBJECT_NOT_IN_CHAIN",
        "MISSING_SNAPSHOT",
        "MISSING_REGION",
        "MISSING_RECOVERY_TIME",
        "UNKNOWN_NODE",
        "NODE_REGION_NOT_ALLOWED",
        "SNAPSHOT_NOT_FOUND",
        "SNAPSHOT_CYCLE",
        "CHAIN_NOT_FULL",
        "SNAPSHOT_REGION_FORBIDDEN",
        "SNAPSHOT_WINDOW_CLOSED",
        "INVALID_RECOVERY_WINDOW",
        "EMPTY_PERMISSION_INTERSECTION",
        "RECOVERY_INSTANT_OUTSIDE_PERMISSION_WINDOW",
    }
    action_technical_failure = any(
        violation.get("code") in technical_codes for violation in action_report.get("violations", [])
    )
    recoverable = (
        all(obj.get("recoverable") for obj in object_reports)
        and not any(f.get("code") in technical_codes for f in failures)
        and not action_technical_failure
    )

    action_failure_codes = [v.get("code") for v in action_report.get("violations", [])]
    approved = recoverable and not failures and not action_report.get("violations")
    result.update(
        {
            "recoverable": recoverable,
            "approved": approved,
            "failure_evidence": failures,
            "action_violation_codes": action_failure_codes,
        }
    )
    return result


def node_conflicts(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for i, left in enumerate(actions):
        if not left.get("node") or not left.get("region"):
            continue
        for right in actions[i + 1 :]:
            if left.get("node") != right.get("node") or not right.get("region"):
                continue
            if left.get("region") == right.get("region"):
                continue
            same_window = left.get("window_id") == right.get("window_id")
            overlap = intervals_overlap(left.get("interval"), right.get("interval"))
            if not same_window and not overlap:
                continue
            conflicts.append(
                {
                    "code": "NODE_REGION_CONFLICT",
                    "message": "one recovery node cannot use two regions in the same recovery window",
                    "node": left["node"],
                    "window_id": left.get("window_id") if same_window else None,
                    "overlapping_intervals": overlap,
                    "actions": [
                        {
                            "action_id": left["action_id"],
                            "candidate_id": left.get("candidate_id"),
                            "region": left["region"],
                            "window_id": left.get("window_id"),
                            "recovery_at": left["at"].isoformat() if left.get("at") else None,
                        },
                        {
                            "action_id": right["action_id"],
                            "candidate_id": right.get("candidate_id"),
                            "region": right["region"],
                            "window_id": right.get("window_id"),
                            "recovery_at": right["at"].isoformat() if right.get("at") else None,
                        },
                    ],
                }
            )
    return conflicts


def evaluate_plan(case: Any, plan: dict[str, Any]) -> dict[str, Any]:
    raw_actions = plan.get("actions", plan.get("plan_actions", plan.get("selected_actions", [])))
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    actions = [normalize_action(raw, case, plan, i) for i, raw in enumerate(raw_actions or [])]
    action_reports = [evaluate_action(case, action) for action in actions]
    conflicts = node_conflicts(actions)
    conflicted_action_ids = {
        participant["action_id"] for conflict in conflicts for participant in conflict.get("actions", [])
    }

    assignments: list[dict[str, Any]] = []
    for action, report in zip(actions, action_reports):
        for wsid in action.get("work_sets", []):
            assignment = evaluate_assignment(case, action, report, wsid)
            if action["action_id"] in conflicted_action_ids:
                assignment["approved"] = False
                assignment["node_region_conflict"] = True
            assignments.append(assignment)

    assignment_by_ws: dict[str, list[dict[str, Any]]] = {wsid: [] for wsid in case.work_set_ids}
    for assignment in assignments:
        assignment_by_ws.setdefault(assignment["work_set_id"], []).append(assignment)

    work_set_summaries = []
    recoverable_entities: list[dict[str, Any]] = []
    approved_ws_count = 0
    recoverable_ws_count = 0
    for wsid, ws in case.work_sets.items():
        rows = assignment_by_ws.get(wsid, [])
        recoverable = any(row.get("recoverable") for row in rows)
        approved = any(row.get("approved") for row in rows)
        covered_by = [row["action_id"] for row in rows]
        if recoverable:
            recoverable_ws_count += 1
            recoverable_entities.append({"entity_type": "work_set", "id": wsid, "status": "recoverable"})
        if approved:
            approved_ws_count += 1
        object_ids = ws.object_ids
        work_set_summaries.append(
            {
                "work_set_id": wsid,
                "covered_by_actions": covered_by,
                "recoverable": recoverable,
                "approved": approved,
                "object_ids": object_ids,
                "mandatory_ids": ws.mandatory_ids,
                "assignments": rows,
            }
        )
        for oid in object_ids:
            obj_reports = [obj for row in rows for obj in row.get("objects", []) if obj.get("object_id") == oid]
            live = next((o for o in obj_reports if o.get("recoverable")), None)
            if live:
                recoverable_entities.append(
                    {
                        "entity_type": "object",
                        "id": oid,
                        "work_set_id": wsid,
                        "status": live.get("status"),
                        "source_snapshot": live.get("source_snapshot"),
                        "snapshot_chain": live.get("snapshot_chain"),
                    }
                )

    failures: list[dict[str, Any]] = []
    for report in action_reports:
        failures.extend(report.get("violations", []))
    failures.extend(conflicts)
    for assignment in assignments:
        for evidence in assignment.get("failure_evidence", []):
            failures.append(
                {
                    "work_set_id": assignment["work_set_id"],
                    "action_id": assignment["action_id"],
                    **evidence,
                }
            )
    for wsid, rows in assignment_by_ws.items():
        if wsid in case.work_sets and not rows:
            evidence = {
                "code": "WORK_SET_UNCOVERED",
                "work_set_id": wsid,
                "message": "plan does not assign this work set to any action",
            }
            failures.append(evidence)

    all_covered = all(assignment_by_ws.get(wsid) for wsid in case.work_set_ids)
    approved = not failures and all_covered and not conflicts

    return {
        "schema": "vaultdrill.evaluation.v1",
        "case_id": case.case_id,
        "plan_id": plan.get("id") or plan.get("plan_id"),
        "summary": {
            "work_set_count": len(case.work_sets),
            "planned_assignment_count": len(assignments),
            "recoverable_work_sets": recoverable_ws_count,
            "approved_work_sets": approved_ws_count,
            "all_work_sets_covered": all_covered,
            "action_count": len(actions),
            "violation_or_failure_count": len(failures),
            "approved": approved,
        },
        "recoverable_entities": recoverable_entities,
        "actions": action_reports,
        "node_region_conflicts": conflicts,
        "work_sets": work_set_summaries,
        "assignments": assignments,
        "failures": failures,
        "minimal_failure_evidence": failures,
    }
