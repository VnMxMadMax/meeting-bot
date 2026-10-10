"""Short-term memory — ONE meeting, carried across calls.

Without it every call is a cold start: Otto re-proposes commitments it already proposed
from an overlapping transcript slice, "what did I just pick up?" sees only the current
slice, and "can THIS task fit?" has no antecedent.

Records, not prose. A summary ("Hammadur took on an API task") loses the minutes, the
seat and the confirmation state — the very things every later decision needs. The model
never writes anything here.

Nothing across meetings: Otto_Requirements.md excludes Episodic and Long-Term memory, and
the 24h TTL is what keeps this from quietly becoming them.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .config import create_driver, logger
from .core import _otto_norm, otto_session_candidates   # noqa: F401  (re-exported)
from .helpers import _f, _i

OTTO_SESSION_TTL_HOURS = 24

def _otto_session_now() -> int:
    return int(time.time())

def _otto_title_key(title: Optional[str]) -> str:
    """Two proposals are 'the same commitment' when assignee + this + minutes match."""
    return _otto_norm(title)[:120]

def load_otto_session(meeting_id: str, account_id: str) -> Dict[str, Any]:
    """The session for this meeting, or a fresh one.

    A session older than OTTO_SESSION_TTL_HOURS is NOT resumed — it is reported as
    expired and the caller starts clean. A meeting_id reused next week must not inherit
    last week's proposals; that would be cross-meeting memory, which the requirements
    exclude.
    """
    blank = {"meeting_id": meeting_id, "account_id": account_id, "turn": 0,
             "transcript_chars": 0, "transcript_hash": None,
             "proposals": [], "answers": [], "placements": [],
             "started_at": _otto_session_now(), "expired_from": None}

    driver = create_driver()
    if not driver:
        raise HTTPException(status_code=500, detail="Neo4j unavailable")
    try:
        with driver.session() as session:
            row = session.execute_read(lambda tx: tx.run("""
                MATCH (s:OttoSession {meeting_id: $mid})
                RETURN s.account_id       AS account_id,
                       s.turn             AS turn,
                       s.transcript_chars AS transcript_chars,
                       s.transcript_hash  AS transcript_hash,
                       s.proposals        AS proposals,
                       s.answers          AS answers,
                       s.placements       AS placements,
                       s.started_at       AS started_at,
                       s.updated_at       AS updated_at
            """, mid=meeting_id).single())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("load_otto_session failed meeting=%s: %s", meeting_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        driver.close()

    if not row:
        return blank

    age_hours = (_otto_session_now() - _i(row.get("updated_at"), 0)) / 3600.0
    if age_hours > OTTO_SESSION_TTL_HOURS:
        logger.info("otto session expired meeting=%s age=%.1fh", meeting_id, age_hours)
        return dict(blank, expired_from=round(age_hours, 1))

    # A session belongs to one account. Another account reaching the same meeting_id gets
    # a clean session rather than someone else's proposals.
    if row.get("account_id") and row["account_id"] != account_id:
        logger.warning("otto session account mismatch meeting=%s", meeting_id)
        return blank

    def _load(field):
        raw = row.get(field)
        if not raw:
            return []
        try:
            out = json.loads(raw)
            return out if isinstance(out, list) else []
        except ValueError:
            logger.warning("otto session %s unreadable meeting=%s", field, meeting_id)
            return []

    return {"meeting_id": meeting_id, "account_id": account_id,
            "turn": _i(row.get("turn"), 0),
            "transcript_chars": _i(row.get("transcript_chars"), 0),
            "transcript_hash": row.get("transcript_hash"),
            "proposals": _load("proposals"), "answers": _load("answers"),
            "placements": _load("placements"),
            "started_at": _i(row.get("started_at"), _otto_session_now()),
            "expired_from": None}

def save_otto_session(sess: Dict[str, Any]) -> bool:
    """Persist the session, attached to the Meeting node when one exists.

    meeting.py already creates (m:Meeting {id}) with participants and invitations, so Otto
    hangs off that rather than inventing a second meeting concept. A meeting_id with no
    Meeting node still works — the demo console passes one — the session is simply
    unattached.
    """
    driver = create_driver()
    if not driver:
        logger.error("save_otto_session: Neo4j unavailable")
        return False
    try:
        with driver.session() as session:
            session.execute_write(lambda tx: tx.run("""
                MERGE (s:OttoSession {meeting_id: $mid})
                  SET s.account_id       = $account_id,
                      s.turn             = $turn,
                      s.transcript_chars = $chars,
                      s.transcript_hash  = $hash,
                      s.proposals        = $proposals,
                      s.answers          = $answers,
                      s.placements       = $placements,
                      s.started_at       = coalesce(s.started_at, $started_at),
                      s.updated_at       = $now
                WITH s
                OPTIONAL MATCH (m:Meeting {id: $mid})
                FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END |
                         MERGE (m)-[:HAS_OTTO_SESSION]->(s))
            """,
                mid=sess["meeting_id"], account_id=sess.get("account_id"),
                turn=_i(sess.get("turn"), 0), chars=_i(sess.get("transcript_chars"), 0),
                hash=sess.get("transcript_hash"),
                proposals=json.dumps(sess.get("proposals") or [], ensure_ascii=False),
                answers=json.dumps(sess.get("answers") or [], ensure_ascii=False),
                placements=json.dumps(sess.get("placements") or [], ensure_ascii=False),
                started_at=_i(sess.get("started_at"), _otto_session_now()),
                now=_otto_session_now()))
        return True
    except Exception as e:
        logger.error("save_otto_session failed meeting=%s: %s",
                     sess.get("meeting_id"), e, exc_info=True)
        return False

def merge_otto_proposals(sess: Dict[str, Any],
                         fresh: List[Dict[str, Any]],
                         turn: int) -> Dict[str, Any]:
    """Fold this turn's proposals into the session, suppressing ones already seen.

    A client sending overlapping transcript slices — or a person pressing the button twice
    — re-extracts the same commitment. Without this, the same task is proposed again under
    a new id, and "what did I just pick up?" counts it twice.

    Sameness is (assignee, normalised title, minutes). Deliberately strict: a genuinely
    re-stated commitment with a different duration IS a new proposal, because the duration
    is what gets booked.
    """
    seen = {}
    for p in (sess.get("proposals") or []):
        if p.get("superseded_by"):
            continue
        seen[(_otto_norm(p.get("assignee_name")), _otto_title_key(p.get("title")),
              _f(p.get("minutes"), -1))] = p

    kept, suppressed = [], []
    for p in fresh:
        key = (_otto_norm(p.get("assignee_name")), _otto_title_key(p.get("title")),
               _f(p.get("minutes"), -1))
        prior = seen.get(key)
        if prior:
            suppressed.append({"proposal_id": p.get("proposal_id"),
                               "title": p.get("title"),
                               "already_proposed_as": prior.get("proposal_id"),
                               "first_seen_turn": prior.get("turn")})
            continue
        row = {k: p.get(k) for k in
               ("proposal_id", "title", "assignee_name", "seat_id", "minutes",
                "status", "evidence_quote")}
        row["turn"] = turn
        row["superseded_by"] = None
        row["outcome"] = None
        kept.append(row)
        seen[key] = row

    sess["proposals"] = (sess.get("proposals") or []) + kept
    return {"new": kept, "suppressed": suppressed}

def record_otto_placement(meeting_id: str, account_id: str,
                          placement: Dict[str, Any]) -> bool:
    """Note on the session that a proposal was actually written to a calendar.

    Kept out of confirm_otto_placement's own path: a placement must succeed or fail on the
    calendar alone, and a session write that fails must never make a real booking look like
    it did not happen.
    """
    if not meeting_id:
        return False
    try:
        sess = load_otto_session(meeting_id, account_id)
    except HTTPException:
        return False

    sess["placements"] = (sess.get("placements") or []) + [placement]
    pid = placement.get("source_proposal_id")
    if pid:
        for p in sess.get("proposals") or []:
            if p.get("proposal_id") == pid:
                p["outcome"] = "placed"
                break
    return save_otto_session(sess)
