"""Small shared pieces: coercion, the weekly capacity table, plan unwrapping."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .config import logger

_WEEK_CAPACITY = {
    "part_time": {"story_points": 7,  "fte": 0.5, "min_minutes": 960,  "max_minutes": 1200,
                  "non_task_minutes": 130, "task_minutes": 1070},
    "full_time": {"story_points": 14, "fte": 1.0, "min_minutes": 1920, "max_minutes": 2400,
                  "non_task_minutes": 480, "task_minutes": 1920},
}

# RATED WEEKS (Cash Flow Statement tab, cols AC-AJ). Every profile sums to 7 — that is
# the part-time week. A FULL-TIME week is TWO profiles paired, summing to 14; the
# workbook's 13-week sprint sequence pairs them explicitly (rows 43-55).


def _f(v, default=0.0):
    """float() that survives None, "" and junk — these values arrive from a UI form."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _i(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default

def _load(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {}

def _plan_root(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The events level of a stored plan, accepting either shape it arrives in.

    store_seat_calendar_batch persists the whole per-seat projection, so the blob read
    back carries seat / week_rollup / points_reconciliation alongside calendar_events, and
    the events + block_scaffold + minutes_budget sit INSIDE calendar_events. A caller
    holding just the calendar_events dict (which is what the calendar endpoint passes
    around internally) is equally valid. Unwrap one level when it is there.
    """
    if isinstance(plan, dict) and isinstance(plan.get("calendar_events"), dict):
        return plan["calendar_events"]
    return plan if isinstance(plan, dict) else {}

def _week_capacity_for(capacity_points: float) -> Dict[str, Any]:
    """
    Given a seat's story-point capacity, find the corresponding weekly capacity configuration.
    """
    
    for row in _WEEK_CAPACITY.values():
        if row["story_points"] == capacity_points:
            return row
        
    return _WEEK_CAPACITY["full_time"]


def derive_story_point(duration_minutes):
    """Duration -> Fibonacci story point. Verbatim from tools/story_point_calendar.py."""
    from .story_points import derive_story_point as _d
    return _d(duration_minutes)
