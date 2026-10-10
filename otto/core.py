"""The deterministic half: validate, resolve, ground, decide, answer.

One transcript in, proposals and answers out. The model only extracted — everything here
is computed, which is why the numbers can be trusted.

Each task passes the same gates in order:
  rejected_ungrounded   the evidence quote is not in the transcript
  unresolved_assignee   the name is missing, ambiguous, or not a participant
  no_seat               that participant occupies no seat
  needs_duration        no duration was SPOKEN (an estimate is never booked)
  no_calendar           the seat has no stored calendar
  capacity outcome      fits_this_week | fits_next_week | no_fit_before_deadline |
                        exceeds_capacity
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .access import resolve_seat_accounts
from .capacity import seat_availability_from_plan
from .config import logger
from .graph import fetch_seat_calendar_plan, fetch_seat_occupancy
from .helpers import _f, _i, _week_capacity_for

OTTO_MAX_TRANSCRIPT_CHARS = 50000
OTTO_MAX_PARTICIPANTS = 60
OTTO_MAX_SEATS = 60
_OTTO_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _otto_bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)

def _otto_norm(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

def _otto_quote_found(quote: Optional[str], transcript_norm: str) -> bool:
    q = _otto_norm(quote)
    return bool(q) and q in transcript_norm

def _otto_fmt_min(m: float) -> str:
    m = int(round(m or 0))
    h, r = divmod(m, 60)
    return ("%dh %dm" % (h, r) if r else "%dh" % h) if h else "%dm" % r

def _otto_flatten_transcript(raw: Any) -> str:
    """One transcript as text, whether it arrives as text or as speaker turns.

    Transcription services (and Deepak's meeting pipeline) emit a LIST of turns —
    [{speaker, timestamp, text}, ...] — while the prompt, the evidence_quote grounding
    and OTTO_MAX_TRANSCRIPT_CHARS all work on lines. Flattening here means the model and
    the grounding check still see exactly one format, and a caller never has to join the
    turns itself and risk a different separator than the one the quotes are matched on.

    A turn's text may itself contain newlines (a speaker pausing mid-thought); those are
    kept, because an evidence_quote is matched against ONE line and splitting a turn
    across lines is what lets the model quote the half it means.
    """
    if isinstance(raw, str):
        if not raw.strip():
            raise _otto_bad("transcript is required and must be non-empty text")
        return raw

    if isinstance(raw, list):
        if not raw:
            raise _otto_bad("transcript is required and must be non-empty text")
        lines = []
        for i, turn in enumerate(raw):
            if not isinstance(turn, dict):
                raise _otto_bad("transcript[%d] must be an object with speaker and text" % i)
            text = turn.get("text")
            if not isinstance(text, str) or not text.strip():
                raise _otto_bad("transcript[%d].text is required and must be non-empty" % i)
            speaker = (turn.get("speaker") or "").strip() or "Unknown"
            ts = str(turn.get("timestamp") or "").strip()
            # same shape the prompt's examples use, so quotes ground identically
            lines.append("[%s] %s: %s" % (ts, speaker, text.strip()) if ts
                         else "%s: %s" % (speaker, text.strip()))
        return "\n".join(lines)

    raise _otto_bad("transcript is required and must be non-empty text, or a list of "
                    "{speaker, timestamp, text} turns")

def _otto_seat_context(seat_entry: Dict[str, Any]) -> Dict[str, Any]:
    """Everything Otto needs about one seat, read off its stored per-seat projection."""
    seat = seat_entry.get("seat") or {}
    ce = seat_entry.get("calendar_events") or {}
    capacity = seat.get("capacity_story_points")
    work: Dict[str, float] = {}
    open_slots = []
    for e in ce.get("events") or []:
        kind = e.get("block_type") or "task"
        if kind == "task" and e.get("title"):
            work[e["title"]] = work.get(e["title"], 0.0) + _f(e.get("duration_min"))
        elif kind == "open_work_space":
            open_slots.append({"day_offset": _i(e.get("day_offset"), 0),
                               "start": e.get("start_time"),
                               "end": e.get("books_until"),
                               "minutes": _f(e.get("duration_min"))})
    open_slots.sort(key=lambda s: (s["day_offset"], s["start"] or ""))
    return {
        "seat_id": seat_entry.get("seat_id"),
        "role": seat_entry.get("role") or seat.get("role"),
        "department": seat_entry.get("department") or seat.get("department"),
        "capacity_story_points": capacity,
        "full_week_minutes": _week_capacity_for(capacity)["task_minutes"],
        "plan": seat_entry,
        "plan_week": _i(ce.get("sprint_week"), 1),
        "remaining_minutes": (seat_entry.get("week_rollup") or {}).get("task_minutes_remaining"),
        "objectives": [o.get("objective") for o in seat_entry.get("individual_objectives") or []
                       if o.get("objective")],
        "current_work": [{"title": t, "minutes_this_week": m} for t, m in work.items()],
        "open_slots": open_slots,
    }

def otto_session_candidates(sess: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Proposals that could still be acted on — not placed, not superseded, and real.

    This is what "this task" can refer to. A rejected or unresolved proposal is not a
    candidate: Otto should not resolve a reference to something it already refused.
    """
    live = {"fits_this_week", "fits_next_week", "needs_duration",
            "no_fit_before_deadline", "no_capacity"}
    return [p for p in (sess.get("proposals") or [])
            if p.get("status") in live
            and not p.get("superseded_by")
            and p.get("outcome") != "placed"]

def parse_otto_request(body: Any) -> Dict[str, Any]:
    """Validate one /otto/meeting-tasks request body.

    The body carries only what the backend cannot know: the transcript, who was in the
    room, and which week the meeting belongs to. Everything about a seat — its calendar,
    department, role, capacity and current work — is read from the graph by
    load_otto_seats, because that is what the seat_id is for.

      transcript    required, non-empty. EITHER plain text, OR the turn list a
                    transcription service emits: [{speaker, timestamp, text}, ...],
                    which is flattened here to the same "[t] Speaker: text" lines the
                    prompt already reads. At most OTTO_MAX_TRANSCRIPT_CHARS characters
                    once flattened.
      participants  required, [{name, seat_id|null, is_host}]
      meeting       optional, {sprint_week, day}
      meeting_id    optional, the id of the meeting this transcript belongs to. Present
                    means Otto carries short-term memory across calls for that meeting:
                    it stops re-proposing what it already proposed, and "what did I pick
                    up?" spans the whole meeting rather than the current slice. Absent,
                    Otto is stateless exactly as before.
      extraction    optional, a recorded extraction; present means skip the model

    Raises 400 on any violation so a malformed body never reaches the model.
    """
    if not isinstance(body, dict):
        raise _otto_bad("Body must be a JSON object")

    transcript = _otto_flatten_transcript(body.get("transcript"))
    if len(transcript) > OTTO_MAX_TRANSCRIPT_CHARS:
        raise _otto_bad("transcript over %d characters" % OTTO_MAX_TRANSCRIPT_CHARS)

    raw_participants = body.get("participants")
    if not isinstance(raw_participants, list) or not raw_participants:
        raise _otto_bad("participants is required and must be a non-empty list")
    if len(raw_participants) > OTTO_MAX_PARTICIPANTS:
        raise _otto_bad("participants over %d entries" % OTTO_MAX_PARTICIPANTS)

    participants, seen_names, seen_seats = [], set(), {}
    for i, p in enumerate(raw_participants):
        if not isinstance(p, dict):
            raise _otto_bad("participants[%d] must be an object" % i)

        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _otto_bad("participants[%d].name is required" % i)

        key = _otto_norm(name)
        if key in seen_names:
            raise _otto_bad("participants[%d]: duplicate name %r — Otto could not tell "
                            "them apart when resolving an assignee" % (i, name))
        seen_names.add(key)

        seat_id = p.get("seat_id")
        if seat_id is not None and (not isinstance(seat_id, str) or not seat_id.strip()):
            raise _otto_bad("participants[%d].seat_id must be a seat id or null" % i)
        seat_id = seat_id.strip() if isinstance(seat_id, str) else None

        if seat_id:
            if seat_id in seen_seats:
                raise _otto_bad("participants[%d]: seat %s is already held by %r in this "
                                "meeting" % (i, seat_id, seen_seats[seat_id]))
            seen_seats[seat_id] = name.strip()

        participants.append({"name": name.strip(), "seat_id": seat_id,
                             "is_host": bool(p.get("is_host"))})

    meeting = body.get("meeting")
    if meeting is not None and not isinstance(meeting, dict):
        raise _otto_bad("meeting must be an object")
    meeting = meeting or {}

    sprint_week = meeting.get("sprint_week")
    if sprint_week is not None:
        if isinstance(sprint_week, bool) or not isinstance(sprint_week, (int, float))                 or int(sprint_week) != sprint_week or sprint_week < 1:
            raise _otto_bad("meeting.sprint_week must be a positive whole number")
        sprint_week = int(sprint_week)

    meeting_id = body.get("meeting_id")
    if meeting_id is not None:
        if not isinstance(meeting_id, str) or not meeting_id.strip():
            raise _otto_bad("meeting_id must be a non-empty string")
        meeting_id = meeting_id.strip()
        if len(meeting_id) > 200:
            raise _otto_bad("meeting_id over 200 characters")

    extraction = body.get("extraction")
    if extraction is not None:
        if not isinstance(extraction, dict):
            raise _otto_bad("extraction must be an object")
        if not isinstance(extraction.get("tasks"), list):
            raise _otto_bad("extraction.tasks must be a list")

    return {
        "transcript": transcript,
        "meeting": {"sprint_week": sprint_week, "day": meeting.get("day")},
        "meeting_id": meeting_id,
        "participants": participants,
        "extraction": extraction,
    }

def load_otto_seats(user_id: str, participants: List[Dict[str, Any]],
                    sprint_week: Optional[int] = None) -> Dict[str, Any]:
    """Read every participant's seat from the graph: calendar, role, department.

    This is why a participant carries a seat_id and nothing else. The caller names the
    people in the room; the backend owns what is on their calendars, so Otto fetches it
    here with the same reads the calendar endpoints use (fetch_seat_calendar_plan for the
    plan, fetch_seat_occupancy for role and department).

    A seat whose calendar has not been generated is reported in `missing`, not raised:
    one ungenerated calendar must not stop a meeting from producing proposals for
    everyone else. Those tasks come back `no_calendar`.
    """
    wanted = [p["seat_id"] for p in participants if p.get("seat_id")]
    if not wanted:
        return {"seats": {}, "missing": [], "seat_meta": {}, "unreadable": []}

    # A participant is signed in as themselves, but seats belong to the account that ran
    # the cascade. Read each seat under ITS owner, once the caller has earned access.
    owners = resolve_seat_accounts(user_id, wanted)
    unreadable = sorted(sid for sid in wanted if not owners.get(sid))

    meta = {}
    for account in {o for o in owners.values() if o}:
        try:
            for row in fetch_seat_occupancy(account) or []:
                if row.get("seat_id"):
                    meta[row["seat_id"]] = {"role": row.get("role"),
                                            "department": row.get("department")}
        except HTTPException:
            raise
        except Exception as e:                   # occupancy is context, never the gate
            logger.warning("otto: seat occupancy lookup failed account=%s: %s", account, e)

    seats, missing = {}, []
    for seat_id in wanted:
        owner = owners.get(seat_id)
        if not owner:
            continue                             # reported in `unreadable`, not read
        fetched = fetch_seat_calendar_plan(owner, seat_id, sprint_week=sprint_week)
        plan = fetched.get("plan")
        if not isinstance(plan, dict) or not plan:
            missing.append(seat_id)
            continue

        entry = dict(plan)
        entry["seat_id"] = seat_id
        for field in ("role", "department"):
            if not entry.get(field) and meta.get(seat_id, {}).get(field):
                entry[field] = meta[seat_id][field]

        ctx = _otto_seat_context(entry)
        if fetched.get("plan_sprint_week") is not None:
            ctx["plan_week"] = _i(fetched["plan_sprint_week"], ctx["plan_week"])
        ctx["plan_source"] = fetched.get("plan_source")
        seats[seat_id] = ctx

    logger.info("otto: loaded %d seats, %d missing, %d unreadable", len(seats), len(missing), len(unreadable))

    return {"seats": seats, "missing": missing, "seat_meta": meta,
            "unreadable": unreadable}

def otto_participants_for_prompt(participants: List[Dict[str, Any]],
                                 seats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the model sees about each person: role and current work, NOT free minutes.

    Remaining capacity is withheld on purpose — the model extracts, it does not schedule,
    and a number it can see is a number it will start reasoning about.
    """
    out = []
    for p in participants:
        ctx = seats.get(p.get("seat_id") or "")
        out.append({
            "name": p.get("name"),
            "is_host": bool(p.get("is_host")),
            "role": ctx["role"] if ctx else None,
            "department": ctx["department"] if ctx else None,
            "current_work": [w["title"] for w in ctx["current_work"]] if ctx else [],
            "objectives": ctx["objectives"] if ctx else []
        })
    return out

def resolve_otto_assignee(name: Optional[str],
                          participants: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Spoken name → participant. Full name first, then a UNIQUE first-name match.

    Two people sharing a first name resolve to nobody rather than to the first one found
    — a wrong assignee is worse than a question.
    """
    n = _otto_norm(name)
    if not n:
        return {"match": "none"}
    exact = [p for p in participants if _otto_norm(p.get("name")) == n]
    if len(exact) == 1:
        return {"match": "full_name", "participant": exact[0]}
    first = n.split()[0]
    by_first = [p for p in participants if _otto_norm(p.get("name")).split()[:1] == [first]]
    if len(by_first) == 1:
        return {"match": "first_name", "participant": by_first[0]}
    if len(by_first) > 1:
        return {"match": "ambiguous", "candidates": [p.get("name") for p in by_first]}
    return {"match": "none"}

def _otto_week_asked_for(request: Dict[str, Any], plan_week: int) -> Optional[int]:
    """Which sprint week the person asked about, or None.

    Prefers the model's `target_week`, but falls back to reading the question itself.
    The model classifies the request type reliably and omits newer optional fields often
    enough that depending on one silently answers about the wrong week — which reads as
    Otto ignoring the question.
    """
    raw = request.get("target_week")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in ("this_week", "current", "current_week"):
            return plan_week
        if key in ("next_week", "next"):
            return plan_week + 1
        m = re.search(r"(\d+)", key)
        if m:
            return int(m.group(1))

    text = " ".join(str(request.get(k) or "") for k in ("question", "evidence_quote"))
    text = _otto_norm(text)
    if not text:
        return None
    m = re.search(r"week\s+(\d+)", text)          # "week 2"
    if m:
        return int(m.group(1))
    if "next week" in text:
        return plan_week + 1
    if "this week" in text or "current week" in text:
        return plan_week
    return None

def build_otto_task_proposals(extraction: Dict[str, Any], transcript: str,
                              participants: List[Dict[str, Any]],
                              seats: Dict[str, Dict[str, Any]],
                              session_proposals: Optional[List[Dict[str, Any]]] = None
                              ) -> Dict[str, Any]:
    """
    Build one validated proposal for each extracted task.

    The function is deterministic and read-only. Each task passes through these
    validation gates in order:
    rejected_ungrounded   — evidence_quote is not found in the transcript
    unresolved_assignee   — assignee is missing, ambiguous, or not a participant
    no_seat               — assigned participant does not occupy a seat
    needs_duration        — task duration was not explicitly stated
    no_calendar           — assigned seat has no stored calendar
    capacity outcome      — fits_this_week | fits_next_week |
                            no_fit_before_deadline | exceeds_capacity

    Tasks are evaluated in meeting order. A per-seat ledger tracks minutes already
    claimed by earlier tasks so capacity checks account for previously proposed work.

    No calendar or task assignments are written by this function.
    """
    transcript_norm = _otto_norm(transcript)
    booked: Dict[str, Dict[int, float]] = {}
    proposals = []

    for idx, t in enumerate(extraction.get("tasks") or [], start=1):
        p = {
            "proposal_id": "OTTO_T%d" % idx, 
            "title": t.get("title"),
            "description": t.get("description"),
            "assignee_name": t.get("assignee_name"),
            "assignee_source": t.get("assignee_source"),
            "assignee_reason": t.get("assignee_reason"),
            "minutes": t.get("minutes"), 
            "minutes_source": t.get("minutes_source"),
            "deadline_text": t.get("deadline_text"),
            "deadline_week_relative": t.get("deadline_week"),
            "evidence_quote": t.get("evidence_quote"),
            "accepted_quote": t.get("accepted_quote"),
            "related_current_work": t.get("related_current_work"),
            "confidence": t.get("confidence"),
            "seat_id": None, 
            "role": None, 
            "status": None, 
            "capacity": None, 
            "flags": []
        }
        proposals.append(p)

        if not _otto_quote_found(t.get("evidence_quote"), transcript_norm):
            p["status"] = "rejected_ungrounded"
            p["flags"].append("evidence_quote not found in transcript")
            continue

        if t.get("accepted_quote") and not _otto_quote_found(t["accepted_quote"], transcript_norm):
            p["flags"].append("accepted_quote not found in transcript — ignored")
            p["accepted_quote"] = None

        who = resolve_otto_assignee(
            t.get("assignee_name"), 
            participants
        )

        if who["match"] == "ambiguous":
            p["status"] = "unresolved_assignee"
            p["flags"].append("ambiguous name, candidates: %s" % ", ".join(who["candidates"]))
            continue

        if who["match"] == "none":
            p["status"] = "unresolved_assignee"
            p["flags"].append("not a meeting participant — needs a seat assignment")
            continue

        seat_id = who["participant"].get("seat_id")

        if not seat_id:
            p["status"] = "no_seat"
            p["flags"].append("participant occupies no seat")
            continue
        
        ctx = seats.get(seat_id)
        p["seat_id"], p["role"] = seat_id, ctx["role"] if ctx else None

        rel = t.get("related_current_work")
        if rel and ctx and rel not in [w["title"] for w in ctx["current_work"]]:
            p["flags"].append("related_current_work is not on this seat's calendar — cleared")
            p["related_current_work"] = None

        minutes = t.get("minutes")
        if t.get("minutes_source") != "stated" or isinstance(minutes, bool) \
                or not isinstance(minutes, (int, float)) or minutes <= 0:
            p["status"] = "needs_duration"
            p["flags"].append("no duration stated — ask the owner before booking")
            continue

        if not ctx:
            p["status"] = "no_calendar"
            continue

        deadline_week = {
            "this_week": ctx["plan_week"],
            "next_week": ctx["plan_week"] + 1
        }.get(t.get("deadline_week"))

        if minutes > ctx["full_week_minutes"]:
            cap = {"seat_id": seat_id, "outcome": "exceeds_capacity",
                   "task_minutes": minutes, "full_week_task_minutes": ctx["full_week_minutes"]}
        else:
            cap = seat_availability_from_plan(
                seat_id, 
                ctx["full_week_minutes"], 
                ctx["plan"],
                ctx["plan_week"], 
                minutes,
                deadline_week=deadline_week,
                booked=booked.get(seat_id)
            )

        p["capacity"], p["status"] = cap, cap["outcome"]
        if cap["outcome"] in ("fits_this_week", "fits_next_week"):
            ledger = booked.setdefault(seat_id, {})
            ledger[cap["sprint_week"]] = ledger.get(cap["sprint_week"], 0) + minutes

    counts: Dict[str, int] = {}
    for p in proposals:
        counts[p["status"]] = counts.get(p["status"], 0) + 1

    answers = answer_otto_requests(
        extraction.get("otto_requests") or [], 
        transcript_norm,
        participants, 
        seats, 
        proposals, 
        booked,
        session_proposals=session_proposals)
    
    return {
        "proposals": proposals,
        "otto_answers": answers,
        "not_tasks": extraction.get("not_tasks") or [],
        "model_audit": extraction.get("audit") or [],
        "status_counts": counts,
        "minutes_proposed_by_seat": booked,
        "writes_calendar": False
    }

def answer_otto_requests(requests: List[Dict[str, Any]], transcript_norm: str,
                         participants: List[Dict[str, Any]],
                         seats: Dict[str, Dict[str, Any]],
                         proposals: List[Dict[str, Any]],
                         booked: Dict[str, Dict[int, float]],
                         session_proposals: Optional[List[Dict[str, Any]]] = None
                         ) -> List[Dict[str, Any]]:
    """One answer per request the model classified. Deterministic, read-only.

    Status per answer:
      answered             the calendar gave a real answer
      needs_clarification  "can THIS task fit?" with more than one task it could mean —
                           Otto lists them rather than guessing, because a wrong guess
                           books the wrong thing
      rejected_ungrounded  the quote is not in the transcript
      unresolved_person    the person it is about is not a participant / is ambiguous
      no_seat / no_calendar  nothing to read
      unsupported          Otto cannot answer this type yet — said plainly, not guessed

    "Bookable" is the Gap 2 budget (task_minutes_remaining), which is the only number the
    capacity check will honour. Open slots are shown too, but they are larger — the
    calendar leaves open space the scheduler never counts as bookable work time — so the
    headline is always the bookable figure.
    """
    answers = []
    for idx, r in enumerate(requests, start=1):
        a = {"answer_id": "OTTO_Q%d" % idx, "asked_by": r.get("asked_by"),
             "about_name": r.get("about_name"), "request_type": r.get("request_type"),
             "question": r.get("question"), "evidence_quote": r.get("evidence_quote"),
             "status": None, "answer": None, "data": None, "flags": []}
        answers.append(a)

        if not _otto_quote_found(r.get("evidence_quote"), transcript_norm):
            a["status"] = "rejected_ungrounded"
            a["answer"] = "Otto could not find this question in the transcript."
            continue
        rtype = r.get("request_type")
        if rtype not in ("free_slots", "current_work", "can_fit", "meeting_tasks"):
            a["status"] = "unsupported"
            a["answer"] = ("Otto can't answer that from the calendar yet. It can answer free "
                           "time, current work, whether a task fits, and what was assigned "
                           "in this meeting.")
            continue

        who = resolve_otto_assignee(r.get("about_name") or r.get("asked_by"), participants)
        if who["match"] in ("none", "ambiguous"):
            a["status"] = "unresolved_person"
            a["answer"] = "Otto doesn't know who “%s” is in this meeting." % (
                r.get("about_name") or r.get("asked_by"))
            continue
        person = who["participant"]
        name = person.get("name")
        asker = resolve_otto_assignee(r.get("asked_by"), participants)
        if asker.get("participant") is not person:
            # Gap 4 (whose calendar may a participant read?) is still undecided — answer in
            # the demo, but make it visible rather than quietly allowed.
            a["flags"].append("asked about another person's calendar — access rule (Gap 4) "
                              "not decided yet")

        if rtype == "meeting_tasks":
            # "what did I just pick up?" means the whole MEETING, not the slice that
            # happened to be sent this call. With a session, earlier turns count too;
            # without one, this is the current turn exactly as before.
            scope = proposals
            if session_proposals is not None:
                seen_ids = {p.get("proposal_id") for p in proposals}
                earlier = [p for p in session_proposals
                           if p.get("proposal_id") not in seen_ids]
                scope = earlier + list(proposals)
            mine = [p for p in scope if p.get("seat_id") and p["seat_id"] == person.get("seat_id")]
            a["status"] = "answered"
            a["data"] = {"tasks": [{"proposal_id": p["proposal_id"], "title": p["title"],
                                    "minutes": p["minutes"], "status": p["status"]}
                                   for p in mine]}
            a["answer"] = ("%s picked up %d task%s in this meeting: %s." % (
                name, len(mine), "" if len(mine) == 1 else "s",
                "; ".join(p["title"] for p in mine)) if mine
                else "%s was not given any tasks in this meeting." % name)
            continue

        seat_id = person.get("seat_id")
        if not seat_id:
            a["status"] = "no_seat"
            a["answer"] = "%s doesn't occupy a seat, so there is no calendar to read." % name
            continue
        ctx = seats.get(seat_id)
        if not ctx:
            a["status"] = "no_calendar"
            a["answer"] = "There is no calendar for %s's seat yet." % name
            continue

        week = ctx["plan_week"]
        before = ctx["remaining_minutes"]
        if before is None:
            before = ctx["full_week_minutes"]
        proposed = (booked.get(seat_id) or {}).get(week, 0)
        after = before - proposed

        if rtype == "free_slots":
            slots = [dict(s, day=_OTTO_WEEKDAYS[s["day_offset"] % 7]) for s in ctx["open_slots"]]
            open_total = sum(s["minutes"] for s in slots)
            a["status"] = "answered"
            a["data"] = {"sprint_week": week, "open_slots": slots,
                         "open_slot_count": len(slots), "open_minutes": open_total,
                         "bookable_minutes_before_meeting": before,
                         "proposed_this_meeting_minutes": proposed,
                         "bookable_minutes_after_meeting": after}
            a["answer"] = ("%s has %s of bookable work time left in week %d%s. "
                           "The calendar shows %d open slot%s (%s in total)." % (
                name, _otto_fmt_min(after), week,
                (" after the %s proposed in this meeting (%s before)" % (
                    _otto_fmt_min(proposed), _otto_fmt_min(before)) if proposed else ""),
                len(slots), "" if len(slots) == 1 else "s", _otto_fmt_min(open_total)))
            continue

        if rtype == "current_work":
            work = ctx["current_work"]
            used = ctx["full_week_minutes"] - before
            a["status"] = "answered"
            a["data"] = {"sprint_week": week, "current_work": work,
                         "booked_minutes": used, "full_week_minutes": ctx["full_week_minutes"],
                         "proposed_this_meeting_minutes": proposed}
            a["answer"] = ("%s's week %d has %s of %s booked: %s.%s" % (
                name, week, _otto_fmt_min(used), _otto_fmt_min(ctx["full_week_minutes"]),
                "; ".join("%s (%s)" % (w["title"], _otto_fmt_min(w["minutes_this_week"]))
                          for w in work) or "nothing yet",
                (" Plus %s proposed in this meeting." % _otto_fmt_min(proposed)) if proposed else ""))
            continue

        # can_fit
        minutes = r.get("minutes")
        stated = not (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
                      or minutes <= 0)

        # "can THIS task be added?" — the size was never spoken, so it comes from what
        # Otto proposed earlier in this meeting. A STATED size always wins: a number in
        # the sentence is what the person meant, whatever memory holds.
        if not stated:
            # No flag needed. A can_fit with no stated size can only be about something
            # already discussed — that is what makes it answerable at all.
            #
            # THIS TURN counts too. When someone is given a task and asks about it in the
            # same breath — "...ninety minutes" ... "can this be added to this week?" —
            # the task is in `proposals`, not yet in the session. Reading only the
            # session answered needs_duration for the most natural way to ask.
            pool = list(session_proposals or []) + list(proposals or [])
            by_id = {}
            for c in pool:
                if c.get("proposal_id"):
                    by_id[c["proposal_id"]] = c          # this turn's copy wins
            mine = [c for c in otto_session_candidates({"proposals": list(by_id.values())})
                    if c.get("seat_id") == seat_id
                    and isinstance(c.get("minutes"), (int, float))
                    and not isinstance(c.get("minutes"), bool)
                    and c["minutes"] > 0]

            if len(mine) == 1:
                # exactly one candidate — resolve it, and NAME it, so a person can see
                # immediately if Otto picked the wrong task
                minutes = float(mine[0]["minutes"])
                stated = True
                a["resolved_from"] = {"proposal_id": mine[0].get("proposal_id"),
                                      "title": mine[0].get("title"),
                                      "minutes": minutes}
            elif len(mine) > 1:
                # two or more — asking is cheaper than booking the wrong one
                a["status"] = "needs_clarification"
                a["data"] = {"candidates": [
                    {"proposal_id": c.get("proposal_id"), "title": c.get("title"),
                     "minutes": c.get("minutes")} for c in mine]}
                a["answer"] = "Which task — %s?" % " or ".join(
                    "%s (%s)" % (c.get("title"), _otto_fmt_min(c.get("minutes")))
                    for c in mine)
                continue

        if not stated:
            a["status"] = "needs_duration"
            a["answer"] = "How long is the task? Otto needs a size before it can check."
            continue

        # "can this go in NEXT week?" must be answered about next week. Without this the
        # answer is always about the seat's current plan week, which quietly contradicts
        # the question that was asked.
        asked_week = _otto_week_asked_for(r, week)

        if asked_week and asked_week != week:
            a["asked_about_week"] = asked_week
        if minutes > ctx["full_week_minutes"]:
            cap = {"seat_id": seat_id, "outcome": "exceeds_capacity",
                   "task_minutes": minutes, "full_week_task_minutes": ctx["full_week_minutes"]}
        elif asked_week and asked_week > week:
            # A later week has nothing packed into it yet, so the whole week is open.
            # Saying so is honest; pretending the current week's numbers apply is not.
            cap = {"seat_id": seat_id, "outcome": "fits_future_week",
                   "sprint_week": asked_week, "task_minutes": minutes,
                   "remaining_minutes": ctx["full_week_minutes"],
                   "note": ("week %d is not generated yet; reported as fully open because "
                            "nothing has been placed into it" % asked_week)}
        else:
            cap = seat_availability_from_plan(seat_id, ctx["full_week_minutes"], ctx["plan"],
                                              week, minutes, booked=booked.get(seat_id))
        a["status"] = "answered"
        a["data"] = {"task_minutes": minutes, "capacity": cap}
        said = ("\u201c%s\u201d (%s)" % (a["resolved_from"]["title"], _otto_fmt_min(minutes))
                if a.get("resolved_from") else _otto_fmt_min(minutes))
        a["answer"] = {
            "fits_this_week": "Yes — %s fits in week %d; %s has %s bookable left." % (
                said, week, name, _otto_fmt_min(cap.get("remaining_minutes"))),
            "fits_next_week": "Not this week (only %s left), but it fits in week %d." % (
                _otto_fmt_min(cap.get("current_week_remaining")), cap.get("sprint_week") or week + 1),
            "fits_future_week": "Yes — %s fits in week %d. Nothing is scheduled there yet, "
                                "so the whole %s week is open." % (
                said, cap.get("sprint_week") or 0,
                _otto_fmt_min(ctx["full_week_minutes"])),
            "exceeds_capacity": "No — %s is more than a whole week (%s). Split it first." % (
                _otto_fmt_min(minutes), _otto_fmt_min(ctx["full_week_minutes"])),
        }.get(cap["outcome"], "No room found for %s." % _otto_fmt_min(minutes))
    return answers
