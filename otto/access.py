"""Whose calendar may this caller read? (Gap 4)

Seats and their calendars belong to the ACCOUNT that ran the cascade, but a meeting
participant is signed in as themselves. Scoping the read to the caller's own token finds
nothing, and every seat comes back "no calendar" — which is the bug the app hit.

So the owner is resolved from the seat, and access is then earned one of three ways:
the caller IS the owning account, the caller OCCUPIES that seat, or the caller is on a
team the owner owns. Anything else is refused, not silently read.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .config import create_driver, logger

def resolve_seat_accounts(caller_user_id: str,
                          seat_ids: List[str]) -> Dict[str, Optional[str]]:
    """seat_id -> the account whose cascade owns it, when the caller may read it.

    Seats and their calendars belong to the ACCOUNT that ran the cascade, but a meeting
    participant is signed in as themselves. Scoping the read to the caller's own token
    finds nothing and every seat comes back `no_calendar` — which is what the app hit.

    So the owner is resolved from the seat, and access is then earned one of three ways:
      the caller IS the owning account
      the caller OCCUPIES that seat               (Gap 1's edge)
      the caller is on a team the owner owns      (same check /assign-seats makes)

    Not blanket access: a seat on an account the caller has nothing to do with resolves
    to None and is reported as unreadable rather than silently read. That keeps this
    inside the fallback Otto_Requirements.md §Gap-4 names — access granted at team setup,
    not implied by joining a session.
    """
    if not seat_ids:
        return {}

    driver = create_driver()
    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    try:
        with driver.session() as session:
            rows = session.execute_read(lambda tx: tx.run("""
                UNWIND $seats AS sid
                MATCH (st:Seat {seat_id: sid})
                WITH sid, st.user_id AS owner
                OPTIONAL MATCH (occ:User {user_id: $caller})-[:OCCUPIES]->(:Seat {seat_id: sid})
                OPTIONAL MATCH (:User {user_id: owner})-[:OWNS_TEAMS]->(:Teams)
                               <-[:HAS_TEAM]-(t:Team)<-[:BELONGS_TO]-(:User {user_id: $caller})
                RETURN sid                 AS seat_id,
                       owner               AS owner,
                       occ IS NOT NULL     AS occupies,
                       count(t) > 0        AS on_team
            """, seats=list(seat_ids), caller=caller_user_id).data())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("resolve_seat_accounts failed caller=%s: %s",
                     caller_user_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        driver.close()

    out: Dict[str, Optional[str]] = {sid: None for sid in seat_ids}
    for r in rows:
        owner = r.get("owner")
        allowed = (owner == caller_user_id) or r.get("occupies") or r.get("on_team")
        out[r["seat_id"]] = owner if allowed else None
        if owner and not allowed:
            logger.info("otto: caller %s may not read seat %s (owned by %s)",
                        caller_user_id, r["seat_id"], owner)
    return out

def seat_account_for(caller_user_id: str, seat_id: str) -> str:
    """The account whose calendar to read for this seat, for this caller.

    A seat belongs to the account that ran the cascade, but a participant is signed in
    as themselves — so reading under the caller's own id finds nothing and every seat
    looks like it has no calendar. Resolve the owner, once the caller has earned access.

    Raises 403 rather than returning the caller's id: a seat the caller may not read
    must say so, not quietly come back empty as though it did not exist.
    """
    owner = resolve_seat_accounts(caller_user_id, [seat_id]).get(seat_id)
    if not owner:
        raise HTTPException(
            status_code=403,
            detail="no access to seat %s — the caller is not on the account that owns it"
                   % seat_id)
    return owner
