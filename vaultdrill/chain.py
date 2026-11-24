from __future__ import annotations

from typing import Any

from .timeutil import parse_dt, parse_window


def build_chain(case: Any, selected_id: str | None) -> dict[str, Any]:
    if selected_id in (None, ""):
        return {
            "selected_snapshot": None,
            "snapshot_chain": [],
            "snapshots": [],
            "terminal_snapshot": None,
            "full_snapshot": None,
            "errors": [{"code": "MISSING_SNAPSHOT", "message": "action does not specify a snapshot"}],
        }

    selected_id = str(selected_id)
    ids: list[str] = []
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = selected_id

    while current:
        if current in seen:
            cycle_start = ids.index(current) if current in ids else 0
            cycle = [*ids[cycle_start:], current]
            errors.append(
                {
                    "code": "SNAPSHOT_CYCLE",
                    "message": "snapshot parent references form a cycle",
                    "snapshot_cycle": cycle,
                }
            )
            break
        seen.add(current)
        snap = case.snapshots.get(current)
        if snap is None:
            errors.append(
                {
                    "code": "SNAPSHOT_NOT_FOUND",
                    "message": f"snapshot {current!r} is absent from the immutable snapshot directory",
                    "missing_snapshot": current,
                    "referenced_by": ids[-1] if ids else None,
                    "snapshot_chain": ids,
                }
            )
            break
        ids.append(current)
        snapshots.append(snap)
        current = snap.get("parent")

    terminal = snapshots[-1] if snapshots else None
    full_id: str | None = None
    if terminal is not None and not errors:
        if terminal.get("kind") == "full":
            full_id = terminal["id"]
        else:
            errors.append(
                {
                    "code": "CHAIN_NOT_FULL",
                    "message": "selected increment does not terminate at a full snapshot",
                    "selected_snapshot": selected_id,
                    "terminal_snapshot": terminal["id"],
                    "snapshot_chain": ids,
                }
            )

    return {
        "selected_snapshot": selected_id,
        "snapshot_chain": ids,
        "snapshots": snapshots,
        "terminal_snapshot": terminal["id"] if terminal else None,
        "full_snapshot": full_id,
        "errors": errors,
    }


def check_snapshot_access(snap: dict[str, Any], region: str | None, at: Any) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    sid = snap["id"]
    regions = snap.get("regions") or []
    if region and regions and "*" not in regions and region not in regions:
        failures.append(
            {
                "code": "SNAPSHOT_REGION_FORBIDDEN",
                "message": f"snapshot {sid} is not accessible from region {region!r}",
                "snapshot": sid,
                "requested_region": region,
                "accessible_regions": regions,
            }
        )

    raw_window = snap.get("recovery_window")
    if raw_window not in (None, ""):
        try:
            window = parse_window(raw_window, owner=sid, label="recovery_window")
        except ValueError as exc:
            failures.append(
                {
                    "code": "INVALID_RECOVERY_WINDOW",
                    "message": str(exc),
                    "snapshot": sid,
                    "window": raw_window,
                }
            )
            window = None
        if window is not None:
            if at is None:
                failures.append(
                    {
                        "code": "MISSING_RECOVERY_TIME",
                        "message": "recovery time is required to check the snapshot recovery window",
                        "snapshot": sid,
                        "recovery_window": window.to_json(),
                    }
                )
            elif not window.contains(at):
                failures.append(
                    {
                        "code": "SNAPSHOT_WINDOW_CLOSED",
                        "message": f"snapshot {sid} recovery window does not contain the recovery instant",
                        "snapshot": sid,
                        "recovery_at": at.isoformat() if hasattr(at, "isoformat") else at,
                        "recovery_window": window.to_json(),
                    }
                )
    return failures


def resolve_object(case: Any, selected_id: str | None, object_id: str) -> dict[str, Any]:
    chain = build_chain(case, selected_id)
    winner: tuple[dict[str, Any], dict[str, Any], int] | None = None
    marker: tuple[dict[str, Any], dict[str, Any], int] | None = None
    masked_copy: tuple[dict[str, Any], dict[str, Any]] | None = None
    table_trace: list[dict[str, Any]] = []

    for index, snap in enumerate(chain["snapshots"]):
        entry = snap["objects"].get(object_id)
        delete = snap["delete_markers"].get(object_id)
        row: dict[str, Any] = {
            "snapshot": snap["id"],
            "depth_from_selected": index,
            "access_rule_version": snap.get("access_rule_version"),
        }
        if entry is not None:
            row["table_state"] = "live"
            row["digest"] = entry.get("digest")
            row["name"] = entry.get("name")
            if winner is None and marker is None:
                winner = (snap, entry, index)
                row["decision"] = "wins"
            elif marker is not None and masked_copy is None:
                masked_copy = (snap, entry)
                row["decision"] = "masked_by_delete_marker"
            else:
                row["decision"] = "older_version_ignored"
        elif delete is not None:
            row["table_state"] = "delete_marker"
            if winner is None and marker is None:
                marker = (snap, delete, index)
                row["decision"] = "delete_marker_masks_inherited_copies"
            else:
                row["decision"] = "superseded_by_newer_live_entry"
        else:
            row["table_state"] = "absent"
            row["decision"] = "continue_to_parent"
        table_trace.append(row)

    result: dict[str, Any] = {
        "object_id": object_id,
        "selected_snapshot": chain["selected_snapshot"],
        "snapshot_chain": chain["snapshot_chain"],
        "full_snapshot": chain["full_snapshot"],
        "source_snapshot": None,
        "status": "unrecoverable",
        "recoverable": False,
        "name": None,
        "digest": None,
        "access_rule_version": None,
        "delete_marker_snapshot": marker[0]["id"] if marker else None,
        "masked_copy_snapshot": masked_copy[0]["id"] if masked_copy else None,
        "masked_copy_name": masked_copy[1].get("name") if masked_copy else None,
        "masked_copy_digest": masked_copy[1].get("digest") if masked_copy else None,
        "table_trace": table_trace,
        "failure_evidence": list(chain["errors"]),
    }

    if chain["errors"]:
        # Chain errors are already minimal structural evidence.
        return result

    if winner is not None:
        snap, entry, _ = winner
        result.update(
            {
                "status": "live",
                "recoverable": True,
                "source_snapshot": snap["id"],
                "name": entry.get("name"),
                "digest": entry.get("digest"),
                "access_rule_version": snap.get("access_rule_version"),
            }
        )
        return result

    if marker is not None:
        marker_snap, marker_entry, _ = marker
        evidence = {
            "code": "OBJECT_DELETED_BY_MARKER",
            "message": (
                "nearest table entry is a delete marker; it masks copies on the inherited parent path"
            ),
            "object_id": object_id,
            "delete_marker_snapshot": marker_snap["id"],
            "snapshot_chain": chain["snapshot_chain"],
        }
        if masked_copy is not None:
            evidence["masked_inherited_copy_snapshot"] = masked_copy[0]["id"]
            evidence["masked_name"] = masked_copy[1].get("name")
            evidence["masked_digest"] = masked_copy[1].get("digest")
        result["failure_evidence"].insert(0, evidence)
        result["status"] = "deleted"
        return result

    terminal = chain["terminal_snapshot"]
    evidence = {
        "code": "OBJECT_NOT_IN_CHAIN",
        "message": "no live entry or delete marker for stable object id on the full parent chain",
        "object_id": object_id,
        "terminal_snapshot": terminal,
        "snapshot_chain": chain["snapshot_chain"],
    }
    result["failure_evidence"].insert(0, evidence)
    result["status"] = "absent"
    return result


def materialize_view(case: Any, selected_id: str | None) -> dict[str, Any]:
    chain = build_chain(case, selected_id)
    decided: set[str] = set()
    live: dict[str, dict[str, Any]] = {}
    markers: dict[str, dict[str, Any]] = {}

    for snap in chain["snapshots"]:
        for oid, marker in snap["delete_markers"].items():
            if oid not in decided:
                decided.add(oid)
                markers[oid] = {"marker": marker, "snapshot": snap["id"]}
        for oid, entry in snap["objects"].items():
            if oid not in decided:
                decided.add(oid)
                live[oid] = {"entry": entry, "snapshot": snap["id"], "access_rule_version": snap.get("access_rule_version")}

    return {"chain": chain, "live": live, "delete_markers": markers}
