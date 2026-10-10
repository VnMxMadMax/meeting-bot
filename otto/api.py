"""Otto's HTTP surface — the same four endpoints the main app exposes.

Each one is thin on purpose: read the body, call the deterministic half, return what it
produced. Every decision lives in core.py, capacity.py and placement.py, so this file
can be read in one sitting.

Identity comes from the bearer token, as it does everywhere else in the codebase. The one
exception is /seat-lookup, which takes a user_id in the body so a tool can resolve any
person's seat; that is deliberate and called out below.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .config import logger
from .core import (build_otto_task_proposals, load_otto_seats,
                   otto_participants_for_prompt, parse_otto_request)
from .capacity import check_seat_availability
from .graph import fetch_seat_for_user, fetch_seat_occupancy
from .llm import invoke_extraction, summarize_usages
from .placement import confirm_otto_placement, propose_otto_placement
from .session import load_otto_session, merge_otto_proposals, save_otto_session

app = FastAPI(title="Otto", description="Meeting transcript → proposed tasks")


# ── auth ─────────────────────────────────────────────────────────────────────
async def get_current_user(request: Request) -> Dict[str, Any]:
    """Decode the bearer token. Mirrors rag/auth.py without the refresh machinery —
    this package is for running and understanding Otto, not for issuing tokens."""
    import jwt
    from .config import JWT_ALGORITHM, JWT_SECRET_KEY

    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        return jwt.decode(header[7:], JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def process_token_payload(payload: Dict[str, Any]):
    """user_id out of a verified payload, plus any refreshed tokens to echo back."""
    new_tokens = {}
    if isinstance(payload, dict) and "new_access_token" in payload:
        new_tokens = {"new_access_token": payload.get("new_access_token"),
                      "new_refresh_token": payload.get("new_refresh_token")}
        payload = payload.get("payload")
    if not payload or payload.get("sub") is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID missing in token")
    return new_tokens, user_id


async def _read_json(request: Request, what: str) -> Dict[str, Any]:
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="A JSON body is required — %s" % what)
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body


# ── the main call ────────────────────────────────────────────────────────────
@app.post("/otto/meeting-tasks")
async def otto_meeting_tasks(request: Request, payload: dict = Depends(get_current_user)):
    """Transcript → proposed tasks + answers. Writes nothing.

    Body:
      transcript    required — the meeting text, or [{speaker, timestamp, text}, ...]
      participants  required — [{name, seat_id, is_host}]
      meeting       optional — {sprint_week, day}
      meeting_id    optional — carries short-term memory across calls for one meeting
      extraction    optional — a recorded extraction; present means the model is skipped
    """
    new_tokens, user_id = process_token_payload(payload)
    body = await _read_json(request, "send transcript and participants")

    otto_input = parse_otto_request(body)

    loaded = load_otto_seats(user_id, otto_input["participants"],
                             sprint_week=otto_input["meeting"]["sprint_week"])
    otto_input["seats"] = loaded["seats"]

    # Short-term memory, only when the caller named a meeting. A session failure must
    # never cost a caller their extraction, so the load is guarded.
    meeting_id = otto_input.get("meeting_id")
    session = None
    if meeting_id:
        try:
            session = load_otto_session(meeting_id, user_id)
        except Exception as e:
            logger.warning("otto session unavailable meeting=%s: %s", meeting_id, e)

    supplied = otto_input["extraction"]
    prior = (session or {}).get("proposals") if session else None

    response_body = {
        "message": "Otto meeting tasks (read-only)",
        "source": "supplied_extraction" if supplied else "live_extraction",
        "meeting": otto_input["meeting"],
        "errors": [],
    }

    if supplied:
        result = build_otto_task_proposals(supplied, otto_input["transcript"],
                                           otto_input["participants"], otto_input["seats"],
                                           session_proposals=prior)
        result.update({"status": "success", "usages": [], "raw_extraction": supplied})
    else:
        result = await _extract_and_build(user_id, otto_input, prior)

    response_body["token_usage"] = {"summary": summarize_usages(result.pop("usages", []))}
    if result.get("status") != "success":
        response_body["errors"].append(result.get("error"))
    response_body.update(result)
    response_body["participants"] = otto_input["participants"]
    response_body["seats"] = {
        sid: {"role": c["role"], "department": c["department"],
              "plan_week": c["plan_week"],
              "remaining_minutes_before_meeting": c["remaining_minutes"],
              "full_week_minutes": c["full_week_minutes"]}
        for sid, c in otto_input["seats"].items()}

    # Fold this turn into the session. A proposal matching an earlier turn is SUPPRESSED
    # rather than returned again — the same commitment re-extracted from an overlapping
    # slice is not a second task.
    if session is not None and result.get("status") == "success":
        turn = session.get("turn", 0) + 1
        session["turn"] = turn
        session["transcript_chars"] = len(otto_input["transcript"])
        merged = merge_otto_proposals(session, response_body.get("proposals") or [], turn)

        if merged["suppressed"]:
            repeats = {r["proposal_id"] for r in merged["suppressed"]}
            response_body["proposals"] = [p for p in response_body["proposals"]
                                          if p.get("proposal_id") not in repeats]
            counts = {}
            for p in response_body["proposals"]:
                counts[p["status"]] = counts.get(p["status"], 0) + 1
            response_body["status_counts"] = counts

        if not save_otto_session(session):
            response_body["errors"].append(
                "the meeting session could not be saved — this turn is not remembered")

        response_body["session"] = {
            "meeting_id": meeting_id, "turn": turn,
            "known_proposals": len(session.get("proposals") or []),
            "new_this_turn": len(merged["new"]),
            "repeats_suppressed": merged["suppressed"],
            "placed": sum(1 for p in (session.get("proposals") or [])
                          if p.get("outcome") == "placed"),
            "transcript_chars_seen": session["transcript_chars"],
            "resumed": turn > 1,
            "expired_from_hours": session.get("expired_from"),
        }

    if loaded["missing"]:
        response_body["errors"].append(
            "no generated calendar for seat(s): %s — generate their calendar first"
            % ", ".join(loaded["missing"]))
    if loaded.get("unreadable"):
        response_body["errors"].append(
            "no access to seat(s): %s — the caller is not on the account that owns them"
            % ", ".join(loaded["unreadable"]))

    if new_tokens:
        response_body.update(new_tokens)
    return response_body


async def _extract_and_build(user_id, otto_input, session_proposals):
    """One model pass, then the deterministic gates."""
    prompt_inputs = {
        "meeting": json.dumps(otto_input["meeting"], ensure_ascii=False),
        "participants": json.dumps(
            otto_participants_for_prompt(otto_input["participants"], otto_input["seats"]),
            ensure_ascii=False, indent=1),
        "transcript": otto_input["transcript"],
    }
    extraction, usage, err = await invoke_extraction(prompt_inputs, user_id)
    if err or extraction is None:
        return {"status": "failed", "error": err or "extraction returned nothing",
                "usages": [usage]}

    result = build_otto_task_proposals(extraction, otto_input["transcript"],
                                       otto_input["participants"], otto_input["seats"],
                                       session_proposals=session_proposals)
    result.update({"status": "success", "usages": [usage], "raw_extraction": extraction})
    return result


# ── the write half ───────────────────────────────────────────────────────────
@app.post("/otto/propose-placement")
async def otto_propose_placement(request: Request,
                                 payload: dict = Depends(get_current_user)):
    """Where would this task go? Returns a slot and a question. WRITES NOTHING."""
    new_tokens, user_id = process_token_payload(payload)
    body = await _read_json(request, "send seat_id, title and minutes")

    seat_id = body.get("seat_id")
    if not isinstance(seat_id, str) or not seat_id.strip():
        raise HTTPException(status_code=400, detail="seat_id is required")

    day_offset = body.get("day_offset")
    if day_offset is not None:
        if isinstance(day_offset, bool) or not isinstance(day_offset, int) \
                or not 0 <= day_offset <= 6:
            raise HTTPException(status_code=400, detail="day_offset must be 0-6 (0 = Monday)")

    result = propose_otto_placement(
        user_id, seat_id.strip(), body.get("title"), body.get("minutes"),
        sprint_week=body.get("sprint_week"), day_offset=day_offset,
        start_time=body.get("start_time"), description=body.get("description"),
        source_proposal_id=body.get("proposal_id"))

    if new_tokens:
        result.update(new_tokens)
    return result


@app.post("/otto/confirm-placement")
async def otto_confirm_placement(request: Request,
                                 payload: dict = Depends(get_current_user)):
    """The person said yes — add it to the calendar. THIS WRITES."""
    new_tokens, user_id = process_token_payload(payload)
    body = await _read_json(request, "send placement_token")

    token = body.get("placement_token")
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(status_code=400, detail="placement_token is required")

    confirmed_by = body.get("confirmed_by")
    if confirmed_by is not None and not isinstance(confirmed_by, str):
        raise HTTPException(status_code=400, detail="confirmed_by must be text")

    result = confirm_otto_placement(user_id, token.strip(), confirmed_by=confirmed_by)
    if new_tokens:
        result.update(new_tokens)
    return result


# ── supporting reads ─────────────────────────────────────────────────────────
@app.get("/seat-assignments")
async def seat_assignments(payload: dict = Depends(get_current_user)):
    """Every seat on the account with its occupant. Unoccupied seats are included."""
    new_tokens, user_id = process_token_payload(payload)
    seats = fetch_seat_occupancy(user_id)
    body = {"seats": seats, "seats_total": len(seats),
            "seats_unoccupied": sum(1 for s in seats if not s.get("occupant_user_id"))}
    if new_tokens:
        body.update(new_tokens)
    return body


@app.get("/seat-availability/{seat_id}")
async def seat_availability(seat_id: str, request: Request,
                            payload: dict = Depends(get_current_user)):
    """Does a task of size N fit on this seat? (Gap 2)"""
    new_tokens, user_id = process_token_payload(payload)
    qp = request.query_params
    try:
        task_minutes = float(qp["task_minutes"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="task_minutes is required and must be a number")
    deadline_week = None
    if qp.get("deadline_week"):
        try:
            deadline_week = int(qp["deadline_week"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="deadline_week must be an integer")

    result = check_seat_availability(user_id, seat_id, task_minutes,
                                     deadline_week=deadline_week)
    if new_tokens:
        result.update(new_tokens)
    return result


@app.post("/seat-lookup")
async def seat_lookup(request: Request, payload: dict = Depends(get_current_user)):
    """Which seat does this person occupy? seat_id, name, role, department — nothing else.

    Unlike every other endpoint here, the person to look up comes from the BODY rather
    than the token, so any authenticated caller can resolve any user's seat. That is
    deliberate for tooling; it is the one endpoint that does not scope to the caller.
    """
    new_tokens, _ = process_token_payload(payload)
    body = await _read_json(request, "send user_id")

    member_id = body.get("user_id")
    if not isinstance(member_id, str) or not member_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")

    row = fetch_seat_for_user(member_id.strip())
    if not row:
        raise HTTPException(status_code=404, detail="no user with id %s" % member_id.strip())
    if not row.get("seat_id"):
        raise HTTPException(status_code=404, detail="%s occupies no seat"
                            % (row.get("name") or member_id.strip()))

    out = {"seat_id": row["seat_id"], "name": row.get("name"),
           "role": row.get("role"), "department": row.get("department")}
    if new_tokens:
        out.update(new_tokens)
    return out


# ── the test console ─────────────────────────────────────────────────────────
@app.get("/otto/test-ui", include_in_schema=False)
async def otto_test_ui():
    """The console. Served from the API so its calls are same-origin, and uncached —
    a browser serving a stale copy looks exactly like a backend bug."""
    import os
    return FileResponse(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "otto_test.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"})


@app.get("/health", include_in_schema=False)
async def health():
    """Is Otto wired up? Reports what is missing rather than failing opaquely."""
    from .config import missing_settings
    missing = missing_settings()
    out = {"ok": not missing, "missing_settings": missing}
    if not missing:
        try:
            driver = __import__("otto.config", fromlist=["create_driver"]).create_driver()
            try:
                with driver.session() as s:
                    out["neo4j"] = s.run("RETURN 1 AS ok").single()["ok"] == 1
                    out["seats"] = s.run("MATCH (s:Seat) RETURN count(s) AS n").single()["n"]
            finally:
                driver.close()
        except Exception as e:
            out["ok"] = False
            out["neo4j_error"] = str(e)[:200]
    return out
