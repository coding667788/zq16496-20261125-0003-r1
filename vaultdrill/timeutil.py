from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class Window:
    """Half-open UTC interval ``[start, end)``."""

    start: datetime
    end: datetime
    owner: str | None = None
    label: str | None = None

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def to_json(self) -> dict[str, str | None]:
        data = {"start": iso_utc(self.start), "end": iso_utc(self.end)}
        if self.owner is not None:
            data["owner"] = self.owner
        if self.label is not None:
            data["label"] = self.label
        return data


def parse_dt(value: Any, *, field: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        # Unix seconds are accepted for machine-generated fixtures.
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field} is not ISO-8601: {value!r}") from exc
    else:
        raise ValueError(f"{field} is required and must be an ISO-8601 string")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    text = dt.isoformat(timespec="seconds")
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def parse_window(value: Any, *, owner: str | None = None, label: str | None = None) -> Window | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("window must be an object with start and end")
    start_raw = value.get("start", value.get("from", value.get("begin")))
    end_raw = value.get("end", value.get("until", value.get("to")))
    if start_raw in (None, "") or end_raw in (None, ""):
        raise ValueError("window requires start and end")
    start = parse_dt(start_raw, field=f"{label or 'window'}.start")
    end = parse_dt(end_raw, field=f"{label or 'window'}.end")
    return Window(start=start, end=end, owner=owner, label=label)


def moment_from_action(action: dict[str, Any], plan: dict[str, Any] | None = None) -> datetime | None:
    for key in ("at", "recovery_at", "recovered_at", "moment"):
        if action.get(key) not in (None, ""):
            return parse_dt(action[key], field=f"action.{key}")
    window = action.get("window", action.get("recovery_window"))
    if isinstance(window, dict):
        for key in ("at", "start", "from", "begin"):
            if window.get(key) not in (None, ""):
                return parse_dt(window[key], field=f"action.window.{key}")
    if action.get("start") not in (None, ""):
        return parse_dt(action["start"], field="action.start")
    if plan:
        if plan.get("at") not in (None, ""):
            return parse_dt(plan["at"], field="plan.at")
        window = plan.get("window", plan.get("recovery_window"))
        if isinstance(window, dict) and window.get("start") not in (None, ""):
            return parse_dt(window["start"], field="plan.window.start")
    return None


def action_interval(action: dict[str, Any]) -> tuple[datetime, datetime] | None:
    window_value = action.get("window", action.get("recovery_window"))
    window = window_value if isinstance(window_value, dict) else {}
    start_raw = action.get("start", window.get("start") if window else None)
    end_raw = action.get("end", window.get("end") if window else None)
    if start_raw in (None, "") or end_raw in (None, ""):
        return None
    return parse_dt(start_raw, field="action.start"), parse_dt(end_raw, field="action.end")


def intervals_overlap(left: tuple[datetime, datetime] | None, right: tuple[datetime, datetime] | None) -> bool:
    if left is None or right is None:
        return False
    return left[0] < right[1] and right[0] < left[1]


@dataclass
class IntersectionWitness:
    window: Window
    other: Window | None = None

    def to_json(self) -> dict[str, Any]:
        if self.other is None or self.other is self.window:
            return {"windows": [self.window.to_json()], "reason": "window_is_empty"}
        return {
            "windows": [self.window.to_json(), self.other.to_json()],
            "reason": "disjoint_or_touching_windows",
        }


def minimal_empty_intersection(windows: Iterable[Window]) -> IntersectionWitness | None:
    """Return one invalid window or the two windows proving an intersection empty.

    Windows are half-open; touching endpoints have an empty intersection.
    """
    max_start: datetime | None = None
    max_start_window: Window | None = None
    min_end: datetime | None = None
    min_end_window: Window | None = None

    for window in windows:
        if window.start >= window.end:
            return IntersectionWitness(window)

        if max_start is None or window.start > max_start:
            max_start = window.start
            max_start_window = window
        if min_end is None or window.end < min_end:
            min_end = window.end
            min_end_window = window

        assert max_start is not None and min_end is not None
        if max_start >= min_end:
            assert max_start_window is not None and min_end_window is not None
            if max_start_window is min_end_window:
                return IntersectionWitness(max_start_window)
            # The current extremal pair is already a two-window proof.
            return IntersectionWitness(max_start_window, min_end_window)

    return None


def intersect_windows(windows: Iterable[Window]) -> Window | None:
    items = list(windows)
    if not items:
        return None
    start = max(w.start for w in items)
    end = min(w.end for w in items)
    if start >= end:
        return None
    return Window(start=start, end=end, label="intersection")
