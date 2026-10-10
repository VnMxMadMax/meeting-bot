"""The write half: propose a slot, a person confirms, the calendar is updated.

Two steps on purpose. propose works out exactly which open_work_space block a task would
occupy and returns a token describing that write; nothing is stored. confirm takes the
token back and performs it. A person says yes in between, and because the token carries
the resolved slot, the write can only be the placement they were shown.

Placing a task SPLITS the open_work_space block: the task takes the minutes it needs from
the front, the remainder stays open. Leaving the block whole would count the same minutes
as both booked and free, and every later capacity check would read high.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .access import seat_account_for
from .capacity import seat_availability_from_plan
from .config import logger
from .core import _otto_fmt_min, _otto_seat_context, _OTTO_WEEKDAYS
from .graph import fetch_seat_calendar_plan, store_seat_calendar_batch
from .helpers import _f, _i, _plan_root
from .story_points import derive_story_point

OTTO_PLACEMENT_TOKEN_VERSION = 1
OTTO_MIN_PLACEMENT_MINUTES = 5
OTTO_PLACEMENT_SOURCE = "otto_meeting"

def _otto_hhmm_to_min(hhmm: Optional[str]) -> Optional[int]:
    """'09:30' -> 570. None for anything unparseable; callers treat that as unknown."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if 0 <= h < 24 and 0 <= mi < 60 else None

def _otto_min_to_hhmm(total: float) -> str:
    total = int(round(total))
    return "%02d:%02d" % ((total // 60) % 24, total % 60)

def _otto_placement_token(payload: Dict[str, Any]) -> str:
    """An opaque, self-describing handle for one proposed write.

    Not a security boundary — confirm_otto_placement re-validates every field against the
    live calendar before storing. It exists so a confirm names one specific placement a
    person actually saw, rather than a fresh set of arguments the client assembled.
    """
    import base64
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

def _otto_read_token(token: str) -> Dict[str, Any]:
    import base64
    try:
        pad = "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(token + pad).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="placement_token is not readable")
    if not isinstance(data, dict) or data.get("v") != OTTO_PLACEMENT_TOKEN_VERSION:
        raise HTTPException(status_code=400,
                            detail="placement_token is from a different version — "
                                   "ask for a fresh proposal")
    return data

def _otto_open_blocks(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every open_work_space event in a stored plan, in calendar order, with its index.

    The index is the event's position in the stored events list — that is what the confirm
    step edits, so it is carried through the token rather than re-derived from times.
    """
    events = _plan_root(plan).get("events") or []
    out = []
    for idx, e in enumerate(events):
        if (e.get("block_type") or "task") != "open_work_space":
            continue
        start = _otto_hhmm_to_min(e.get("start_time"))
        out.append({
            "event_index": idx,
            "day_offset": _i(e.get("day_offset"), 0),
            "start_time": e.get("start_time"),
            "books_until": e.get("books_until"),
            "minutes": _f(e.get("duration_min")),
            "time_block": e.get("time_block"),
            "_sort": (_i(e.get("day_offset"), 0), start if start is not None else 10 ** 6),
        })
    out.sort(key=lambda b: b["_sort"])
    for b in out:
        b.pop("_sort", None)
    return out

def _otto_pick_open_block(plan: Dict[str, Any], minutes: float,
                          day_offset: Optional[int] = None,
                          start_time: Optional[str] = None) -> Dict[str, Any]:
    """The earliest open_work_space block that can hold `minutes`.

    Earliest-fit, not best-fit: a person reading "Monday morning" expects the first slot
    that works, and leaving the large blocks intact keeps room for the long tasks that
    cannot go anywhere else. An explicit day_offset/start_time pins the choice instead.
    """
    blocks = _otto_open_blocks(plan)
    if not blocks:
        return {"ok": False, "reason": "no_open_work_space",
                "detail": "this week has no open work space left"}

    if day_offset is not None or start_time:
        wanted = [b for b in blocks
                  if (day_offset is None or b["day_offset"] == day_offset)
                  and (not start_time or b["start_time"] == start_time)]
        if not wanted:
            return {"ok": False, "reason": "slot_not_found",
                    "detail": "no open work space at that day and time",
                    "open_blocks": blocks}
        chosen = wanted[0]
        if chosen["minutes"] + 1e-6 < minutes:
            return {"ok": False, "reason": "slot_too_small",
                    "detail": "that slot holds %s, the task needs %s"
                              % (_otto_fmt_min(chosen["minutes"]), _otto_fmt_min(minutes)),
                    "open_blocks": blocks}
        return {"ok": True, "block": chosen, "open_blocks": blocks}

    for b in blocks:
        if b["minutes"] + 1e-6 >= minutes:
            return {"ok": True, "block": b, "open_blocks": blocks}

    largest = max(blocks, key=lambda b: b["minutes"])
    return {"ok": False, "reason": "no_block_large_enough",
            "detail": "the largest open slot is %s, the task needs %s"
                      % (_otto_fmt_min(largest["minutes"]), _otto_fmt_min(minutes)),
            "open_blocks": blocks}

def propose_otto_placement(user_id: str, seat_id: str, title: str, minutes: float,
                           sprint_week: Optional[int] = None,
                           day_offset: Optional[int] = None,
                           start_time: Optional[str] = None,
                           description: Optional[str] = None,
                           source_proposal_id: Optional[str] = None) -> Dict[str, Any]:
    """Work out where a task would go and ask the person to confirm it. WRITES NOTHING.

    Returns either a placement token plus the question to put in front of the person, or a
    refusal naming why — never a silent near-miss. The caller shows `confirm_question`, and
    only if the person agrees does it call confirm_otto_placement with the token.
    """
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        raise HTTPException(status_code=400, detail="minutes must be a number")
    minutes = float(minutes)
    if minutes < OTTO_MIN_PLACEMENT_MINUTES:
        raise HTTPException(status_code=400,
                            detail="minutes must be at least %d" % OTTO_MIN_PLACEMENT_MINUTES)

    user_id = seat_account_for(user_id, seat_id)
    fetched = fetch_seat_calendar_plan(user_id, seat_id, sprint_week=sprint_week)
    plan = fetched.get("plan")
    if not isinstance(plan, dict) or not plan:
        return {"status": "no_calendar", "seat_id": seat_id, "sprint_week": sprint_week,
                "message": "seat %s has no generated calendar%s — generate it before "
                           "placing work on it"
                           % (seat_id, " for week %s" % sprint_week if sprint_week else ""),
                "writes_calendar": False}

    week = _i(fetched.get("plan_sprint_week"), _i(sprint_week, 1))
    ctx = _otto_seat_context(dict(plan, seat_id=seat_id))

    full_week = ctx["full_week_minutes"]
    if minutes > full_week:
        return {"status": "exceeds_capacity", "seat_id": seat_id, "sprint_week": week,
                "task_minutes": minutes, "full_week_task_minutes": full_week,
                "message": "%s is longer than a whole week of work time (%s) — it has to "
                           "be split" % (_otto_fmt_min(minutes), _otto_fmt_min(full_week)),
                "writes_calendar": False}

    remaining = _f(ctx["remaining_minutes"], 0.0)
    if minutes > remaining + 1e-6:
        return {"status": "no_capacity", "seat_id": seat_id, "sprint_week": week,
                "task_minutes": minutes, "bookable_minutes_remaining": remaining,
                "message": "week %d has %s of bookable time left, the task needs %s"
                           % (week, _otto_fmt_min(remaining), _otto_fmt_min(minutes)),
                "writes_calendar": False}

    pick = _otto_pick_open_block(plan, minutes, day_offset=day_offset, start_time=start_time)
    if not pick["ok"]:
        return {"status": pick["reason"], "seat_id": seat_id, "sprint_week": week,
                "task_minutes": minutes, "message": pick["detail"],
                "open_slots": pick.get("open_blocks") or [],
                "writes_calendar": False}

    block = pick["block"]
    start_min = _otto_hhmm_to_min(block["start_time"])
    if start_min is None:
        return {"status": "slot_unreadable", "seat_id": seat_id, "sprint_week": week,
                "message": "that open work space block has no readable start time",
                "writes_calendar": False}

    books_until = _otto_min_to_hhmm(start_min + minutes)
    leftover = round(block["minutes"] - minutes, 2)
    day_name = _OTTO_WEEKDAYS[block["day_offset"] % 7]

    token = _otto_placement_token({
        "v": OTTO_PLACEMENT_TOKEN_VERSION,
        "user_id": user_id, "seat_id": seat_id, "sprint_week": week,
        "event_index": block["event_index"],
        "title": title, "description": description, "minutes": minutes,
        "day_offset": block["day_offset"],
        "start_time": block["start_time"], "books_until": books_until,
        "block_minutes": block["minutes"],
        "source_proposal_id": source_proposal_id,
    })

    return {
        "status": "awaiting_confirmation",
        "seat_id": seat_id, "role": ctx["role"], "department": ctx["department"],
        "sprint_week": week,
        "task": {"title": title, "description": description, "minutes": minutes,
                 "source_proposal_id": source_proposal_id},
        "slot": {"day_offset": block["day_offset"], "day": day_name,
                 "start_time": block["start_time"], "books_until": books_until,
                 "time_block": block["time_block"],
                 "open_block_minutes": block["minutes"],
                 "minutes_left_open_after": leftover},
        "week_after": {"task_minutes_remaining": round(remaining - minutes, 2),
                       "task_minutes_remaining_before": remaining},
        "confirm_question": ("Add “%s” (%s) to %s open work space on %s at %s? "
                             "It would run to %s and leave %s of that slot open."
                             % (title, _otto_fmt_min(minutes), seat_id, day_name,
                                block["start_time"], books_until, _otto_fmt_min(leftover))),
        "placement_token": token,
        "alternatives": [b for b in (pick.get("open_blocks") or [])
                         if b["event_index"] != block["event_index"]][:5],
        "writes_calendar": False,
    }

def confirm_otto_placement(user_id: str, placement_token: str,
                           confirmed_by: Optional[str] = None) -> Dict[str, Any]:
    """Perform a placement a person has confirmed. THIS WRITES THE CALENDAR.

    Every field in the token is re-checked against the calendar as it is NOW. A plan that
    moved since the proposal — the slot filled, the week regenerated, the block shrunk — is
    refused with 409 and the caller proposes again. Nothing is written on any refusal.
    """
    t = _otto_read_token(placement_token)

    seat_id, week = t["seat_id"], _i(t["sprint_week"], 1)

    # The token records the account the calendar belongs to, not the person confirming —
    # a participant signed in as themselves confirms a placement on their account's seat.
    # So the check is "may this caller touch this seat", and the token must name the same
    # account that seat resolves to, which stops a token being replayed against another.
    owner = seat_account_for(user_id, seat_id)
    if t.get("user_id") != owner:
        raise HTTPException(status_code=403,
                            detail="this placement_token belongs to another account")
    user_id = owner
    minutes = _f(t["minutes"])

    fetched = fetch_seat_calendar_plan(user_id, seat_id, sprint_week=week)
    plan = fetched.get("plan")
    if not isinstance(plan, dict) or not plan:
        raise HTTPException(status_code=409,
                            detail="seat %s no longer has a calendar for week %d"
                                   % (seat_id, week))

    root = _plan_root(plan)
    events = root.get("events") or []
    idx = _i(t["event_index"], -1)

    if not (0 <= idx < len(events)):
        raise HTTPException(status_code=409,
                            detail="that open work space block is gone — the calendar "
                                   "changed since the proposal; ask Otto again")

    target = events[idx]
    if (target.get("block_type") or "task") != "open_work_space" \
            or target.get("start_time") != t["start_time"] \
            or _i(target.get("day_offset"), -1) != _i(t["day_offset"], -2):
        raise HTTPException(status_code=409,
                            detail="that slot is no longer the open work space it was — "
                                   "the calendar changed since the proposal; ask Otto again")

    available = _f(target.get("duration_min"))
    if available + 1e-6 < minutes:
        raise HTTPException(status_code=409,
                            detail="that slot now holds only %s, the task needs %s"
                                   % (_otto_fmt_min(available), _otto_fmt_min(minutes)))

    ctx = _otto_seat_context(dict(plan, seat_id=seat_id))
    remaining = _f(ctx["remaining_minutes"], 0.0)
    if minutes > remaining + 1e-6:
        raise HTTPException(status_code=409,
                            detail="week %d now has only %s bookable, the task needs %s"
                                   % (week, _otto_fmt_min(remaining), _otto_fmt_min(minutes)))

    start_min = _otto_hhmm_to_min(target.get("start_time"))
    books_until = _otto_min_to_hhmm(start_min + minutes)
    leftover = round(available - minutes, 2)

    from tools.story_point_calendar import derive_story_point
    sp = derive_story_point(minutes)
    task_id = "OTTO_%s" % uuid.uuid4().hex[:10].upper()

    task_event = {
        "task_id": task_id,
        "parent_task_id": None,
        "task_colour_key": task_id,
        "block_type": "task",
        "title": t["title"],
        "description": t.get("description"),
        "domain": target.get("domain") or "work",
        "story_point": sp["story_point"],
        "duration_min": minutes,
        "time_block": target.get("time_block"),
        "block_hours": target.get("block_hours"),
        "start_time": target.get("start_time"),
        "books_until": books_until,
        "overflows_block": bool(target.get("overflows_block")),
        "activity_count": sp["expected_activities"],
        "activity_slots": [],
        "day_offset": _i(target.get("day_offset"), 0),
        "archetype": None,
        "archetype_name": None,
        "archetype_source": OTTO_PLACEMENT_SOURCE,
        "ui_label": target.get("ui_label"),
        "session_span": sp["session_span"],
        "sessions_needed": sp["sessions_needed"],
        # provenance — this event did not come from the packer
        "placed_by": OTTO_PLACEMENT_SOURCE,
        "placed_at": int(time.time()),
        "confirmed_by": confirmed_by,
        "source_proposal_id": t.get("source_proposal_id"),
    }

    # Split: the task takes the front of the slot, the remainder stays open. A slot used to
    # the minute leaves no remainder, so the OWS block goes rather than lingering at zero.
    new_events = list(events)
    if leftover >= 1.0:
        shrunk = dict(target)
        shrunk["start_time"] = books_until
        shrunk["duration_min"] = leftover
        shrunk["task_id"] = "OWS_D%s_%s" % (_i(target.get("day_offset"), 0),
                                            books_until.replace(":", ""))
        new_events[idx:idx + 1] = [task_event, shrunk]
    else:
        new_events[idx:idx + 1] = [task_event]

    root["events"] = new_events

    budget = root.get("minutes_budget") or {}
    budget["task_minutes_placed"] = round(_f(budget.get("task_minutes_placed")) + minutes, 2)
    budget["open_work_space_minutes"] = round(
        max(0.0, _f(budget.get("open_work_space_minutes")) - minutes), 2)
    root["minutes_budget"] = budget

    rollup = plan.get("week_rollup") or {}
    placed = round(_f(rollup.get("task_minutes_placed")) + minutes, 2)
    available_min = _f(rollup.get("task_minutes_available"), ctx["full_week_minutes"])
    rollup["task_minutes_placed"] = placed
    rollup["task_minutes_remaining"] = round(max(0.0, available_min - placed), 2)
    rollup["open_work_space_minutes"] = budget["open_work_space_minutes"]
    rollup["committed_points"] = _f(rollup.get("committed_points")) + sp["story_point"]
    cap_pt = _f(rollup.get("week_capacity_pt"))
    if cap_pt:
        rollup["remaining_points"] = cap_pt - rollup["committed_points"]
        rollup["over_capacity"] = rollup["committed_points"] > cap_pt
        rollup["utilization_pct"] = round(100.0 * rollup["committed_points"] / cap_pt, 1)
    if available_min:
        rollup["minutes_utilization_pct"] = round(100.0 * placed / available_min, 1)
    plan["week_rollup"] = rollup

    stored = store_seat_calendar_batch(
        user_id, [{"seat_id": seat_id, "calendar": plan}], sprint_week=week)
    if not stored:
        raise HTTPException(status_code=500,
                            detail="the calendar could not be saved — nothing was changed")

    day_name = _OTTO_WEEKDAYS[task_event["day_offset"] % 7]
    return {
        "status": "placed",
        "seat_id": seat_id, "sprint_week": week,
        "task": {"task_id": task_id, "title": t["title"], "minutes": minutes,
                 "story_point": sp["story_point"],
                 "source_proposal_id": t.get("source_proposal_id")},
        "slot": {"day_offset": task_event["day_offset"], "day": day_name,
                 "start_time": task_event["start_time"], "books_until": books_until,
                 "minutes_left_open": leftover},
        "week_after": {"task_minutes_placed": rollup["task_minutes_placed"],
                       "task_minutes_remaining": rollup["task_minutes_remaining"],
                       "committed_points": rollup["committed_points"],
                       "open_work_space_minutes": rollup["open_work_space_minutes"]},
        "confirmed_by": confirmed_by,
        "message": "Added “%s” (%s) to %s on %s at %s."
                   % (t["title"], _otto_fmt_min(minutes), seat_id, day_name,
                      task_event["start_time"]),
        "writes_calendar": True,
    }
