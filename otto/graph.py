"""Every read and write Otto makes against Neo4j.

Seats, their calendars, and who occupies them. Nothing here decides anything — the
decisions live in capacity.py and core.py.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .config import create_driver, logger
from .helpers import _f, _i, _plan_root, _week_capacity_for

def fetch_seat_occupancy(account_id: str) -> List[Dict[str, Any]]:
    """
    Get every seat belonging to the account_id and tells who occupies each seat, if anyone.
    """
    driver = create_driver()

    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    
    try:
        with driver.session() as session:
            rows = session.execute_read(lambda tx: tx.run("""
                MATCH (s:Seat {user_id: $account_id})
                OPTIONAL MATCH (u:User)-[:OCCUPIES]->(s)
                RETURN s.seat_id AS seat_id, s.department AS department,
                       s.role AS role, u.user_id AS occupant_user_id,
                       u.name AS occupant_name, u.email AS occupant_email
                ORDER BY s.department, s.seat_id
            """, account_id=account_id).data())

        return rows
    
    except Exception as e:
        logger.error(
            "fetch_seat_occupancy failed account_id=%s: %s", 
            account_id, 
            e,
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        driver.close()

def fetch_seat_calendar_plan(user_id: str, seat_id: str,
                             sprint_week: Optional[int] = None) -> Dict[str, Any]:
    """
    Fetch the calendar plan for a given seat and sprint week.
    """

    driver = create_driver()
    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    
    try:
        with driver.session() as session:
            rows = session.execute_read(lambda tx: tx.run("""
                MATCH (:Seat {user_id: $user_id, seat_id: $seat_id})
                      -[:HAS_SEAT_CALENDAR]->(sc:SeatCalendar)
                RETURN sc.calendar AS calendar, sc.sprint_week AS sprint_week,
                       sc.timestamp AS ts
            """, user_id=user_id, seat_id=seat_id).data())

    except Exception as e:
        logger.error(
            "fetch_seat_calendar_plan failed user_id=%s seat=%s: %s",
            user_id, 
            seat_id, 
            e, 
            exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        driver.close()

    if not rows:
        return {
            "seat_id": seat_id, 
            "sprint_week": sprint_week, 
            "plan": None,
            "plan_source": None, 
            "plan_sprint_week": None
        }

    want = None if sprint_week is None else _i(sprint_week, 1)

    exact = [r for r in rows if r.get("sprint_week") is not None and _i(r.get("sprint_week"), -1) == want]
    
    legacy = [r for r in rows if r.get("sprint_week") is None]

    pick, src = None, None

    if exact:
        pick, src = max(exact, key=lambda r: r.get("ts") or 0), "sprint_week"

    elif want is None:
        pick, src = max(rows, key=lambda r: r.get("ts") or 0), "latest"

    elif legacy:
        pick, src = max(legacy, key=lambda r: r.get("ts") or 0), "legacy_unkeyed"

    if not pick:
        return {
            "seat_id": seat_id, 
            "sprint_week": want, 
            "plan": None,
            "plan_source": "no_plan_for_week", 
            "plan_sprint_week": None,
            "weeks_stored": sorted({_i(r.get("sprint_week"), 0) for r in rows
                                        if r.get("sprint_week") is not None})}
    try:
        plan = (json.loads(pick["calendar"]) if isinstance(pick["calendar"], str)
                else pick["calendar"])
        
    except (ValueError, TypeError):
        plan = None

    return {
        "seat_id": seat_id,
        "sprint_week": want if want is not None else pick.get("sprint_week"),
        "plan": plan, 
        "plan_source": src,
        "plan_sprint_week": pick.get("sprint_week")
    }

def store_seat_calendar_batch(user_id: str,
                              calendar_results: List[Dict[str, Any]],
                              sprint_week: int = 1) -> bool:
    """Store each seat's projected week under its Seat node:
    (s:Seat)-[:HAS_SEAT_CALENDAR]->(sc:SeatCalendar).

    KEYED BY sprint_week (2026-08-22). It used to merge on {user_id, seat_id} alone,
    so projecting week 2 for a seat overwrote week 1 and the seat could hold exactly one
    week at a time. Before/after reporting needs the budget it is comparing against to
    still be there, and carry-over needs last week's plan, so the week is part of the
    identity now. A node written by the older build has no sprint_week property and will
    not match this MERGE; fetch_seat_calendar_plan falls back to it rather than reading
    the seat as an empty week.

    Separate node and relationship from the mental-action UserCalendar deliberately —
    the two are projections of different work (cost-block activities vs the OKR
    cascade's), and overwriting one with the other would make it impossible to tell which
    input a stored calendar came from. Expects: {seat_id, calendar}.
    """
    rows = [{"seat_id": r.get("seat_id"),
             "calendar": json.dumps(r.get("calendar"), ensure_ascii=False)}
            for r in (calendar_results or [])
            if r.get("seat_id") and r.get("calendar") is not None]
    if not rows:
        logger.warning("store_seat_calendar_batch user_id=%s: nothing to store", user_id)
        return False

    driver = create_driver()
    if not driver:
        logger.error("store_seat_calendar_batch: Neo4j unavailable user_id=%s", user_id)
        return False
    try:
        with driver.session() as session:
            session.execute_write(lambda tx: tx.run("""
                UNWIND $rows AS r
                MATCH (s:Seat {user_id: $user_id, seat_id: r.seat_id})
                MERGE (s)-[:HAS_SEAT_CALENDAR]->(sc:SeatCalendar {user_id: $user_id,
                                                                  seat_id: r.seat_id,
                                                                  sprint_week: $sprint_week})
                  SET sc.calendar = r.calendar, sc.sprint_week = $sprint_week,
                      sc.timestamp = timestamp()
            """, rows=rows, user_id=user_id, sprint_week=int(sprint_week)))
        logger.info("Stored seat calendars user_id=%s seats=%d sprint_week=%s",
                    user_id, len(rows), sprint_week)
        return True
    except Exception as e:
        logger.error("store_seat_calendar_batch failed user_id=%s: %s", user_id, e,
                     exc_info=True)
        return False
    finally:
        driver.close()

def fetch_seat_for_user(member_user_id: str) -> Optional[Dict[str, Any]]:
    """The seat one person occupies: seat_id, name, role, department. Nothing else.

    Deliberately NOT scoped to an account. resolve_user_to_seat needs to know whose
    cascade the seat belongs to, because Otto already has that from the caller's token.
    A client asking "which seat is this person in" usually does not — the OCCUPIES edge
    is unique per user, so the seat is findable without naming the account.

    Role and department come off the SEAT, not the User node. A User carries a role
    string from signup ("Operations Manager") that is unrelated to the seat the cascade
    built, and showing that next to a seat_id would be two different jobs in one row.
    """
    driver = create_driver()
    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    try:
        with driver.session() as session:
            row = session.execute_read(lambda tx: tx.run("""
                MATCH (u:User {user_id: $member_id})
                OPTIONAL MATCH (u)-[:OCCUPIES]->(s:Seat)
                RETURN u.user_id     AS user_id,
                       u.name        AS name,
                       s.seat_id     AS seat_id,
                       s.role        AS role,
                       s.department  AS department
            """, member_id=member_user_id).single())
        return dict(row) if row else None
    except Exception as e:
        logger.error("fetch_seat_for_user failed member=%s: %s",
                     member_user_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        driver.close()
