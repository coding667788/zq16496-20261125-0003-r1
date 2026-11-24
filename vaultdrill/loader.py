from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import VaultDrillError


def read_json_file(path: str | Path) -> Any:
    p = Path(path)
    try:
        # utf-8-sig also accepts a accidental BOM; output is always plain UTF-8.
        with p.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise VaultDrillError("FILE_NOT_FOUND", f"file not found: {p}", details={"path": str(p)}) from exc
    except json.JSONDecodeError as exc:
        raise VaultDrillError(
            "INVALID_JSON",
            f"invalid UTF-8 JSON in {p}: {exc.msg} at line {exc.lineno} column {exc.colno}",
            details={"path": str(p), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise VaultDrillError("IO_ERROR", f"cannot read {p}: {exc}", details={"path": str(p)}) from exc


def write_json_stdout(payload: Any) -> None:
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    sys.stdout.write("\n")


@dataclass
class WorkSet:
    id: str
    object_ids: list[str]
    mandatory_ids: list[str]
    read_groups: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Case:
    path: Path
    root: Path
    case_id: str
    snapshots: dict[str, dict[str, Any]]
    snapshot_dir: Path
    rules: dict[str, Any]
    work_sets: dict[str, WorkSet]
    nodes: dict[str, dict[str, Any]]
    candidates: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def work_set_ids(self) -> list[str]:
        return list(self.work_sets)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _string_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else _as_list(value)
    result: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _mapping_list(raw: Any, *, id_key: str = "id") -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        rows: list[dict[str, Any]] = []
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault(id_key, key)
                rows.append(row)
            else:
                rows.append({id_key: key, "value": value})
        return rows
    if isinstance(raw, list):
        return [dict(row) if isinstance(row, dict) else {id_key: str(row)} for row in raw]
    return []


def normalize_snapshot(raw: dict[str, Any], source_path: Path) -> dict[str, Any]:
    sid = raw.get("id") or raw.get("snapshot_id") or source_path.stem
    sid = str(sid)
    parent = raw.get("parent", raw.get("parent_id", raw.get("parent_ref")))
    parent = None if parent in (None, "") else str(parent)

    kind = raw.get("kind") or raw.get("type") or raw.get("snapshot_type")
    if kind in (None, ""):
        kind = "full" if parent is None or raw.get("full") is True else "incremental"
    kind = str(kind)
    if kind in ("base", "complete"):
        kind = "full"
    elif kind in ("inc", "delta"):
        kind = "incremental"

    objects: dict[str, dict[str, Any]] = {}

    def put_object(oid: Any, value: Any) -> None:
        if oid in (None, ""):
            return
        object_id = str(oid)
        if isinstance(value, str):
            entry = {"id": object_id, "digest": value}
        elif isinstance(value, dict):
            entry = dict(value)
            entry["id"] = object_id
        else:
            entry = {"id": object_id, "value": value}
        if entry.get("deleted") is True or entry.get("delete") is True or entry.get("tombstone") is True:
            markers[object_id] = {"id": object_id, **{k: v for k, v in entry.items() if k != "id"}}
            objects.pop(object_id, None)
        else:
            objects[object_id] = entry

    markers: dict[str, dict[str, Any]] = {}

    # Preferred canonical table: objects: {id: {name, digest}}
    object_table = raw.get("objects", raw.get("object_table"))
    if isinstance(object_table, dict):
        for oid, value in object_table.items():
            put_object(oid, value)
    elif isinstance(object_table, list):
        for value in object_table:
            if isinstance(value, dict):
                oid = value.get("id") or value.get("object_id")
                put_object(oid, value)

    # Convenience aliases for digest/name tables.
    for oid, digest in (raw.get("object_digests") or raw.get("digests") or {}).items() if isinstance(
        raw.get("object_digests") or raw.get("digests"), dict
    ) else []:
        put_object(oid, {"digest": digest, **objects.get(str(oid), {})})
    if isinstance(raw.get("object_names") or raw.get("names"), dict):
        for oid, name in (raw.get("object_names") or raw.get("names")).items():
            if str(oid) in objects:
                objects[str(oid)].setdefault("name", name)
            else:
                put_object(oid, {"name": name})

    delete_raw = raw.get("delete_markers", raw.get("deletions", raw.get("deleted_objects", [])))
    for item in _as_list(delete_raw):
        if isinstance(item, str | int):
            oid = str(item)
            marker = {"id": oid}
        elif isinstance(item, dict):
            oid = item.get("id") or item.get("object_id")
            if oid in (None, ""):
                continue
            oid = str(oid)
            marker = dict(item)
            marker["id"] = oid
        else:
            continue
        markers[oid] = marker
        objects.pop(oid, None)

    window = raw.get("recovery_window", raw.get("window"))
    regions = raw.get("regions", raw.get("accessible_regions", raw.get("region")))
    if isinstance(regions, str):
        regions = [regions]
    elif regions is None:
        regions = []
    else:
        regions = _string_list(regions)

    version = raw.get("access_rule_version", raw.get("rules_version", raw.get("rule_version")))
    return {
        "id": sid,
        "parent": parent,
        "kind": kind,
        "objects": objects,
        "delete_markers": markers,
        "recovery_window": window,
        "regions": regions,
        "access_rule_version": None if version in (None, "") else str(version),
        "path": str(source_path),
        "raw": raw,
    }


def load_snapshot_dir(directory: Path) -> dict[str, dict[str, Any]]:
    if not directory.exists():
        raise VaultDrillError(
            "SNAPSHOT_DIR_NOT_FOUND",
            f"snapshot directory not found: {directory}",
            details={"snapshot_dir": str(directory)},
        )
    if not directory.is_dir():
        raise VaultDrillError(
            "SNAPSHOT_DIR_NOT_DIRECTORY",
            f"snapshot path is not a directory: {directory}",
            details={"snapshot_dir": str(directory)},
        )

    snapshot_paths = list(directory.glob("*.json"))
    snapshot_paths.extend(directory.glob("*/snapshot.json"))
    snapshot_paths.extend(directory.glob("*/manifest.json"))
    snapshots: dict[str, dict[str, Any]] = {}
    for path in sorted(set(snapshot_paths)):
        raw = read_json_file(path)
        if not isinstance(raw, dict):
            raise VaultDrillError("INVALID_SNAPSHOT", "snapshot JSON must be an object", details={"path": str(path)})
        snap = normalize_snapshot(raw, path)
        if snap["id"] in snapshots:
            raise VaultDrillError(
                "DUPLICATE_SNAPSHOT_ID",
                f"duplicate snapshot id {snap['id']!r}",
                details={"snapshot_id": snap["id"], "paths": [snapshots[snap["id"]]["path"], str(path)]},
            )
        snapshots[snap["id"]] = snap
    if not snapshots:
        raise VaultDrillError("NO_SNAPSHOTS", f"no *.json snapshots found in {directory}")
    return snapshots


def load_rules(raw: dict[str, Any], case_root: Path) -> dict[str, Any]:
    rules = raw.get(
        "access_rules",
        raw.get("rules", raw.get("access_rules_file", raw.get("rules_file", {}))),
    )
    if isinstance(rules, str):
        path = Path(rules)
        if not path.is_absolute():
            path = case_root / path
        rules = read_json_file(path)
    if not isinstance(rules, dict):
        raise VaultDrillError("INVALID_RULES", "access_rules must be an object, path, or omitted")
    versions = rules.get("versions", rules.get("rule_versions"))
    if versions is None:
        # Allow {"rv1": {"objects": ...}} as a shorthand.
        versions = rules if all(isinstance(v, dict) for v in rules.values()) else {}
    if not isinstance(versions, dict):
        raise VaultDrillError("INVALID_RULES", "access_rules.versions must be an object")
    return {"versions": versions, "raw": rules}


def load_work_sets(raw: dict[str, Any]) -> dict[str, WorkSet]:
    rows = _mapping_list(raw.get("work_sets", raw.get("working_sets", raw.get("workloads"))), id_key="id")
    result: dict[str, WorkSet] = {}
    for row in rows:
        wsid = str(row.get("id") or row.get("work_set_id") or "")
        if not wsid:
            raise VaultDrillError("INVALID_WORK_SET", "work set requires an id")
        object_ids = _string_list(
            row.get("objects", row.get("object_ids", row.get("required_objects", row.get("mandatory_objects"))))
        )
        mandatory = _string_list(row.get("mandatory_objects", row.get("mandatory_object_ids", row.get("required_objects"))))
        if not mandatory:
            mandatory = list(object_ids)
        # If objects was omitted but mandatory_objects exists, use it as the full set.
        if not object_ids:
            object_ids = list(mandatory)
        groups = _string_list(row.get("read_groups", row.get("groups", row.get("requested_groups"))))
        result[wsid] = WorkSet(
            id=wsid,
            object_ids=object_ids,
            mandatory_ids=mandatory,
            read_groups=groups,
            raw=row,
        )
    return result


def load_nodes(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for row in _mapping_list(raw.get("nodes", []), id_key="id"):
        node_id = str(row.get("id") or row.get("node_id") or "")
        if not node_id:
            continue
        regions = _string_list(row.get("regions", row.get("allowed_regions", [])))
        nodes[node_id] = {"id": node_id, "regions": regions, "raw": row}
    return nodes


def load_case(path: str | Path) -> Case:
    case_path = Path(path).resolve()
    if case_path.is_dir():
        for name in ("case.json", "vaultdrill-case.json", "input.json"):
            candidate = case_path / name
            if candidate.is_file():
                case_path = candidate
                break
        else:
            # A CASE argument may point directly at an immutable snapshot directory.
            # The PLAN can then supply work sets/rules, or the report can inspect actions.
            snapshots = load_snapshot_dir(case_path.resolve())
            return Case(
                path=case_path,
                root=case_path,
                case_id=case_path.name,
                snapshots=snapshots,
                snapshot_dir=case_path.resolve(),
                rules={"versions": {}, "raw": {}},
                work_sets={},
                nodes={},
                candidates=[],
                raw={},
            )
    raw = read_json_file(case_path)
    if not isinstance(raw, dict):
        raise VaultDrillError("INVALID_CASE", "case JSON must be an object", details={"path": str(case_path)})
    root = case_path.parent
    snap_ref = raw.get("snapshot_dir", raw.get("snapshots_dir", raw.get("snapshot_directory", "snapshots")))
    snap_dir = Path(snap_ref)
    if not snap_dir.is_absolute():
        snap_dir = root / snap_dir
    snapshots = load_snapshot_dir(snap_dir.resolve())
    rules = load_rules(raw, root)
    work_sets = load_work_sets(raw)
    nodes = load_nodes(raw)
    candidates = _mapping_list(
        raw.get("candidate_actions", raw.get("candidates", raw.get("actions", []))), id_key="id"
    )
    return Case(
        path=case_path,
        root=root,
        case_id=str(raw.get("id") or raw.get("case_id") or case_path.stem),
        snapshots=snapshots,
        snapshot_dir=snap_dir,
        rules=rules,
        work_sets=work_sets,
        nodes=nodes,
        candidates=candidates,
        raw=raw,
    )


def apply_plan_case_inputs(case: Case, plan: dict[str, Any]) -> Case:
    """Allow PLAN to carry work sets/rules when CASE points at a snapshot dir."""
    scenario = plan.get("case") if isinstance(plan.get("case"), dict) else {}

    if not case.work_sets:
        source = scenario if scenario.get("work_sets") else plan
        if source.get("work_sets") or source.get("working_sets"):
            case.work_sets = load_work_sets(source)

    if not case.nodes:
        source = scenario if scenario.get("nodes") else plan
        if source.get("nodes"):
            case.nodes = load_nodes(source)

    if not case.rules.get("versions"):
        source = scenario if (scenario.get("access_rules") or scenario.get("rules")) else plan
        if source.get("access_rules") or source.get("rules"):
            case.rules = load_rules(source, case.root)

    if not case.raw.get("window") and plan.get("window"):
        case.raw["window"] = plan.get("window")
    if not case.raw.get("default_read_groups") and plan.get("read_groups"):
        case.raw["default_read_groups"] = plan.get("read_groups")
    return case


def load_plan(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    plan_path = Path(path)
    if plan_path.is_dir():
        for name in ("plan.json", "recovery-plan.json", "vaultdrill-plan.json"):
            candidate = plan_path / name
            if candidate.is_file():
                plan_path = candidate
                break
        else:
            raise VaultDrillError(
                "PLAN_JSON_NOT_FOUND",
                f"plan directory {plan_path} must contain plan.json",
                details={"directory": str(plan_path.resolve())},
            )
    raw = read_json_file(plan_path)
    if not isinstance(raw, dict):
        raise VaultDrillError("INVALID_PLAN", "plan JSON must be an object", details={"path": str(plan_path)})
    raw["__path__"] = str(plan_path.resolve())
    return raw
