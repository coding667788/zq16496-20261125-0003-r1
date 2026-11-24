from __future__ import annotations

from typing import Any

from .timeutil import Window, parse_dt, parse_window


def rule_version(case: Any, version: str | None) -> dict[str, Any] | None:
    versions = case.rules.get("versions", {})
    row = versions.get(version) if version else None
    return row if isinstance(row, dict) else None


def object_policy(case: Any, object_id: str, version: str | None) -> tuple[dict[str, Any], str | None, list[dict[str, Any]]]:
    """Return ``(policy, effective_version, evidence)``.

    Secure default when a version/object is absent: no read groups and sealed.
    A case may opt into a different ``default_object_policy``.
    """
    evidence: list[dict[str, Any]] = []
    rv = rule_version(case, version)
    if rv is None:
        evidence.append({"code": "UNKNOWN_RULE_VERSION", "access_rule_version": version})
        default = case.raw.get(
            "default_object_policy",
            {"allowed_groups": [], "sealed": True, "reason": "unknown access rule version; secure default"},
        )
        return dict(default), version, evidence

    objects = rv.get("objects", rv.get("policies", rv.get("object_policies", {})))
    policy = objects.get(object_id) if isinstance(objects, dict) else None
    if policy is None:
        # A known rule version means seal status is explicit policy data. Missing
        # object grant still defaults to no read groups, but does not imply a seal.
        default = rv.get(
            "default_object_policy",
            case.raw.get("default_object_policy", {"allowed_groups": [], "sealed": False}),
        )
        policy = dict(default)
        evidence.append({"code": "OBJECT_POLICY_DEFAULT", "access_rule_version": version})
    elif not isinstance(policy, dict):
        policy = {"allowed_groups": [], "sealed": True}
    else:
        policy = dict(policy)

    # Group-centric grants may live in the same rule version.
    groups = rv.get("groups", rv.get("group_policies", {}))
    if isinstance(groups, dict):
        extra: list[str] = []
        for group_id, grant in groups.items():
            if not isinstance(grant, dict):
                continue
            refs = grant.get("objects", grant.get("may_read", grant.get("object_ids", [])))
            if object_id in {str(x) for x in refs}:
                extra.append(str(group_id))
        if extra:
            existing = policy.get("allowed_groups", policy.get("groups", []))
            merged = list(dict.fromkeys([*[str(x) for x in existing], *extra]))
            policy["allowed_groups"] = merged
    return policy, version, evidence


def policy_window(policy: dict[str, Any], object_id: str) -> Window | None:
    raw = policy.get(
        "permission_window",
        policy.get("license_window", policy.get("read_window", policy.get("window"))),
    )
    return parse_window(raw, owner=object_id, label="permission")


def is_still_sealed(policy: dict[str, Any], at: Any) -> tuple[bool, str | None]:
    if policy.get("sealed") is True or policy.get("status") == "sealed":
        return True, "sealed"
    if policy.get("legal_hold") is True:
        return True, "legal_hold"
    until = policy.get("sealed_until")
    if until not in (None, "") and at is not None:
        unseal = parse_dt(until, field="sealed_until")
        if at < unseal:
            return True, f"sealed_until {unseal.isoformat()}"
    return False, None


def allowed_groups(policy: dict[str, Any]) -> list[str]:
    value = policy.get("allowed_groups", policy.get("groups", policy.get("read_groups", [])))
    if isinstance(value, str):
        value = [value]
    return [str(x) for x in (value or [])]
