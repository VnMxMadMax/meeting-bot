"""Does a task of size N fit on this seat, and if not, when? (Gap 2)

The design turns on one fact: calendars are generated ONE SPRINT WEEK AT A TIME, on
request. There is no quarter-start date in the graph, so "walk forward from today" has
nothing to walk from. The real question is "walk forward from the latest generated week"
— everything after that is uniformly open, which collapses the search to four outcomes
rather than an open-ended loop.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .access import seat_account_for
from .config import create_driver, logger
from .graph import fetch_seat_calendar_plan
from .helpers import _f, _i, _plan_root, _week_capacity_for

def seat_availability_from_plan(seat_id: str, full_week_minutes: float,
                                plan: Dict[str, Any], plan_week: int,
                                task_minutes: float,
                                deadline_week: Optional[int] = None,
                                booked: Optional[Dict[int, float]] = None) -> Dict[str, Any]:
    """check_seat_availability's decision, for a plan already in hand.

    Split out so Otto's demo can run the SAME rule against a calendar snapshot instead of
    a stored SeatCalendar — one decision, two sources, no second copy to drift.

    booked: {sprint_week: minutes} Otto has already proposed onto this seat earlier in the
    same meeting. Without it, two tasks from one meeting would each see the full remaining
    time and both claim it. Omitted, behaviour is exactly the single-task check.
    """
    booked = booked or {}
    rollup = plan.get("week_rollup") or {}
    # task_minutes_remaining is computed at generation time and stored beside the plan;
    # falls back to the full week if the plan predates week_rollup being attached,
    # rather than reporting a false "no room" for an old-shaped stored calendar.
    remaining_this_week = rollup.get("task_minutes_remaining")
    if remaining_this_week is None:
        remaining_this_week = full_week_minutes
    remaining_this_week -= booked.get(plan_week, 0)

    def _meets_deadline(week: int) -> bool:
        return deadline_week is None or week <= deadline_week

    if task_minutes <= remaining_this_week and _meets_deadline(plan_week):
        return {"seat_id": seat_id, "outcome": "fits_this_week",
               "sprint_week": plan_week, "remaining_minutes": remaining_this_week,
               "meets_deadline": True}

    next_week = plan_week + 1
    next_week_open = full_week_minutes - booked.get(next_week, 0)
    if task_minutes <= next_week_open and _meets_deadline(next_week):
        return {"seat_id": seat_id, "outcome": "fits_next_week",
               "sprint_week": next_week, "remaining_minutes": next_week_open,
               "current_week_remaining": remaining_this_week,
               "current_week_checked": plan_week,
               "note": "week %d is not yet generated; reported as fully open because "
                       "nothing has been placed into it" % next_week}

    # fits nowhere before the deadline — this week is short, next week is open but
    # arrives too late. Report both numbers so the caller can decide, never silently
    # push the deadline.
    return {"seat_id": seat_id, "outcome": "no_fit_before_deadline",
           "deadline_week": deadline_week,
           "current_week_checked": plan_week, "current_week_remaining": remaining_this_week,
           "next_open_week": next_week}

def check_seat_availability(
        user_id: str, 
        seat_id: str, 
        task_minutes: float,
        deadline_week: Optional[int] = None) -> Dict[str, Any]:
    """
    Check whether a task fits within a seat's weekly capacity.

    Uses the latest stored calendar for the seat and calculates its weekly task-minute
    capacity from the seat's capacity_story_points.

    Possible outcomes:
    fits_this_week   — the task fits within the available capacity of the latest week
    fits_next_week   — the task does not fit in the latest week but fits within a
                        full week's capacity, so the next week can accommodate it
    no_calendar      — the seat has no stored calendar or required capacity information
    exceeds_capacity — the task is larger than the seat's full weekly task-minute capacity

    This function only checks availability; it does not schedule or place the task.
    """
    driver = create_driver()

    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    
    # The Seat itself belongs to the owning account, so the owner must be resolved
    # BEFORE this lookup — scoping it to the caller finds no seat at all and reports
    # no_calendar, which is indistinguishable from a seat that was never generated.
    user_id = seat_account_for(user_id, seat_id)

    try:
        with driver.session() as session:
            # Find the seat's capacity
            seat_row = session.execute_read(lambda tx: tx.run("""
                MATCH (s:Seat {user_id: $user_id, seat_id: $seat_id})
                RETURN s.capacity_story_points AS capacity_story_points
            """, user_id=user_id, seat_id=seat_id).single())

    finally:
        driver.close()

    if not seat_row or seat_row.get("capacity_story_points") is None:
        return {
            "seat_id": seat_id, 
            "outcome": "no_calendar",
            "reason": "seat not found or has no capacity_story_points"
        }

    week_cap = _week_capacity_for(seat_row["capacity_story_points"])
    full_week_minutes = week_cap["task_minutes"]

    if task_minutes > full_week_minutes:
        return {
            "seat_id": seat_id, 
            "outcome": "exceeds_capacity",
            "task_minutes": task_minutes, 
            "full_week_task_minutes": full_week_minutes
        }

    latest = fetch_seat_calendar_plan(user_id, seat_id, sprint_week=None)
    plan, plan_week = latest.get("plan"), latest.get("plan_sprint_week")

    # plan is None means nothing stored at all; plan == {} is a real (if oddly shaped)
    # stored calendar and must NOT be treated the same as absent — `not plan` conflates
    # them, which is exactly the bug an old-shaped or minimal plan would trigger here.
    if plan is None or plan_week is None:
        return {
            "seat_id": seat_id, 
            "outcome": "no_calendar",
            "reason": "no SeatCalendar stored yet for this seat — run /generate-user-calendar before proposing a task"
        }

    return seat_availability_from_plan(
        seat_id, 
        full_week_minutes, 
        plan, 
        plan_week,
        task_minutes, 
        deadline_week=deadline_week
    )
