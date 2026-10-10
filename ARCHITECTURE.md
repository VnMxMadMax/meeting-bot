# How Otto works

Otto listens to a meeting, works out what each person committed to, and checks whether it
fits on their calendar — before anything is booked.

This document explains the whole thing: what each piece does, why it is built that way,
and where the sharp edges are.

---

## 1. The one idea everything follows from

**The model extracts. Python decides.**

The LLM reads a transcript and reports what was said. It is never shown how much free time
anyone has, and it never schedules anything. Every number a person sees — minutes free,
which week, whether a task fits — is computed in Python from the stored calendar.

That split is not stylistic. An LLM-written *"you have about 20 hours"* is a figure nobody
can check, and if Otto promised room that the capacity check then refused, people would
stop trusting both. So the model's output is treated as a **claim about the transcript**,
and every claim is verified before it reaches a calendar:

| The model says | Otto checks |
|---|---|
| "Sara committed to X" | is that quote actually in the transcript? |
| "it takes 90 minutes" | was that **spoken**, or estimated? Only spoken is booked |
| "assign it to Sara" | is Sara a participant? does she occupy a seat? |
| — | does 90 minutes fit that seat's week? |

---

## 2. The shape of a request

```
                    ┌─────────────────────────────────────────┐
  transcript  ───►  │  1. EXTRACT        (the model, 1 call)  │
  participants      │     tasks · not-tasks · questions       │
  meeting_id        └──────────────────┬──────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────┐
                    │  2. GROUND         deterministic        │
                    │     is the quote really in the text?    │
                    ├─────────────────────────────────────────┤
                    │  3. PERSON → SEAT                       │
                    │     spoken name → participant → seat_id │
                    ├─────────────────────────────────────────┤
                    │  4. CAPACITY                            │
                    │     fits this week / next / not at all  │
                    ├─────────────────────────────────────────┤
                    │  5. ANSWER                              │
                    │     questions asked to Otto, from the   │
                    │     calendar — never from the model     │
                    └──────────────────┬──────────────────────┘
                                       │
                            proposals + answers
                         (nothing written yet)
```

Steps 2–5 never call the model. Given the same extraction they produce the same result
every time, which is why a recorded extraction can replay a meeting for free.

---

## 3. The files

```
otto/
  config.py        credentials, the Neo4j driver
  prompt.py        the extraction prompt, verbatim
  llm.py           the one model call + token accounting
  helpers.py       _f / _i coercion, the weekly capacity table
  story_points.py  Fibonacci story points from a duration
  graph.py         every read and write against Neo4j
  access.py        whose calendar may this caller read?        (Gap 4)
  capacity.py      does a task of size N fit?                  (Gap 2)
  core.py          validate → ground → resolve → decide → answer
  placement.py     propose a slot, a person confirms, write it
  session.py       short-term memory for one meeting
  api.py           the HTTP endpoints
  static/          the test console
```

Dependencies point one way: `api → core → capacity → graph → config`. Nothing below
reaches back up.

---

## 4. The request body, and why it is that small

```jsonc
{
  "transcript": "…",
  "participants": [
    { "name": "Dana",            "seat_id": null,                "is_host": true  },
    { "name": "Hammadur Rahman", "seat_id": "SEAT_TECH_…_1",     "is_host": false }
  ],
  "meeting":    { "sprint_week": 1, "day": "Monday" },
  "meeting_id": "09159625-…",      // optional — turns on memory
  "extraction": null               // optional — supply one and the model is skipped
}
```

**There is no `seats` array, and no calendar data.** An earlier version asked the caller to
send each seat's whole calendar projection — 30 KB of it. That was wrong: the backend owns
what is on a calendar, and `seat_id` is the key it looks it up with. A client that has to
assemble calendar data to ask a question about calendars has the responsibility in the
wrong place.

`is_host` is on **every** participant, true or false, so the shape never varies.

Two rules, both 400s:

- **Names must be unique.** Two participants normalising to the same name make every
  assignee resolution ambiguous, so Otto refuses rather than guessing.
- **One seat, one person.** Two participants cannot hold the same `seat_id` — that is one
  calendar, and it would be double-booked silently.

---

## 5. Grounding — why a quote is checked

Every extracted task carries an `evidence_quote`, and Otto checks it is really in the
transcript (case and punctuation insensitive). If it is not, the task is
`rejected_ungrounded` and goes no further.

This is the cheapest possible guard against a confident invention. A model that
hallucinates a commitment still has to hallucinate a quote, and a quote that was never said
does not match. One string comparison removes a whole class of failure.

---

## 6. Person → seat (Gap 1)

A transcript says *"Sara"*. A calendar is keyed by `SEAT_CUSTOMER_OPS_3`. Nothing joined
those two until the `OCCUPIES` edge was added:

```cypher
(u:User)-[:OCCUPIES]->(s:Seat)
```

Resolution is deliberately strict:

| Transcript | Participants | Result |
|---|---|---|
| "Sara" | one Sara | resolved on the first name |
| "Sara" | Sara Malik **and** Sara Khan | `unresolved_assignee` — Otto does not pick |
| "Ali" | nobody called Ali | `unresolved_assignee` — "needs a seat assignment" |

Guessing between two Saras would put real work on the wrong person's week. Asking is
cheaper than being wrong.

---

## 7. Capacity (Gap 2)

The design turns on one fact about this system: **calendars are generated one sprint week
at a time, on request.** There is no quarter-start date anywhere in the graph.

So "walk forward from today" has nothing to walk from. The real question is *"walk forward
from the latest generated week"* — and because nothing has been packed into any week after
that, every later week is uniformly open. An apparently open-ended search collapses into
four outcomes:

| Outcome | Meaning |
|---|---|
| `fits_this_week` | room in the current plan week |
| `fits_next_week` | no room now; the next week is open (and says so in a `note`) |
| `no_fit_before_deadline` | does not fit before the stated deadline — **the decision is handed back**, with the shortfall and the next open week |
| `exceeds_capacity` | bigger than a whole week could hold; needs splitting |

### Two rules a UI must respect

**Only a spoken duration is booked.** If nobody said how long it takes, the result is
`needs_duration`. An estimated number would consume real calendar capacity that nobody
agreed to.

**The ledger is cumulative, in meeting order.** Three tasks on one seat see
`remaining_minutes` of 1420 → 1330 → 1315 — each sees the ones before it. Two 60-minute
tasks on a seat with 100 minutes left give `fits_this_week` then `no_fit_before_deadline`.
**Do not re-sort proposals before showing capacity**, or the numbers stop making sense.

### The gap, not the total, is what decides

A week can have 10 hours free in 30-minute pieces and still refuse a 90-minute task. The
response carries both figures; a UI that shows only the total will look wrong to a person
who was just told "no".

---

## 8. Writing to a calendar

Two steps, on purpose.

```
POST /otto/propose-placement     finds the slot, returns a question + token.  WRITES NOTHING
          ↓     a person says yes
POST /otto/confirm-placement     performs exactly that placement.             WRITES
```

The token carries the resolved slot — seat, week, event index, start time. So a confirm can
only ever perform the placement the person was **shown**; it cannot drift onto a different
block, seat or week. It is not a security boundary (the server re-validates everything), it
is a way of naming one specific proposal.

### What the write does

The open work space block is **split**:

```
BEFORE   OWS  Mon 08:00–12:50   290 min

AFTER    TASK Mon 08:00–09:30    90 min   ← new
         OWS  Mon 09:30–12:50   200 min   ← the remainder, still open
```

An exact fit consumes the block rather than leaving a zero-minute one.

**This matters more than it looks.** Leaving the OWS block whole would count the same
minutes as both booked *and* free, and every later capacity check would read high. The
tests assert `task minutes gained == open minutes lost` for exactly this reason.

### 409 — the calendar moved

Between propose and confirm, the world can change. The token is re-checked against the
calendar as it is **now**, and nothing is written if:

- the slot is no longer open work space (someone else booked it)
- the block shrank below what the task needs
- the week no longer has the bookable minutes
- **the token was already used** — a second confirm cannot double-book

---

## 9. Short-term memory

Without it every call is a cold start. Otto cannot see what it extracted a minute ago, so:

- overlapping transcript slices re-propose the same commitment
- *"what did I just pick up?"* only sees the current slice
- *"can **this** task fit?"* has no antecedent

Send the same `meeting_id` on every call and Otto remembers **one meeting**:

```cypher
(m:Meeting {id})-[:HAS_OTTO_SESSION]->(s:OttoSession {
   meeting_id, account_id, turn, transcript_chars,
   proposals, answers, placements, started_at, updated_at })
```

**Records, not prose.** A summary — *"Hammadur took on an API task"* — loses the minutes,
the seat and the confirmation state, which are the things every later decision needs. The
model never writes anything here.

### What it buys

**No duplicate proposals.** A commitment already proposed is *suppressed* and reported in
`repeats_suppressed`. Sameness is `(assignee, title, minutes)` — a task re-stated with a
**different duration is a new proposal**, because the duration is what gets booked.

**"This task" resolves.** When someone asks *"can this be added to next week?"* without
naming a size, Otto looks at what it has proposed:

| Candidates | Otto |
|---|---|
| exactly one | resolves it, and **names it** in the answer |
| two or more | `needs_clarification` — *"Which one — the API note (90m) or the SSO review?"* |
| none | `needs_duration` |
| — | **a spoken number always wins over a remembered one** |

This includes tasks given **in the same turn**. *"...ninety minutes"* then *"can this be
added?"* in one breath is the most natural way to ask, and the task is in this turn's
proposals rather than in the session yet.

### Nothing across meetings

`Otto_Requirements.md` excludes Episodic and Long-Term memory — Otto has no access to
previous meetings. A session older than **24 hours** is not resumed; the next call on that
`meeting_id` starts clean. That TTL is the only thing keeping this from quietly becoming
cross-meeting memory.

---

## 10. Who may read whose calendar (Gap 4)

Seats and their calendars belong to the **account that ran the cascade**. But a meeting
participant is signed in as **themselves**.

Scoping the lookup to the caller's own token finds nothing, and every seat comes back
"no calendar" — which is exactly the bug the mobile app hit. Otto now resolves the owner
from the seat, and access is **earned**:

| Caller | |
|---|---|
| is the owning account | ✅ |
| **occupies that seat** (Gap 1's edge) | ✅ |
| is on a team the owner owns (the same check `/assign-seats` makes) | ✅ |
| none of those | ❌ `no access to seat(s): …` |

Not blanket access. A refusal is deliberately worded differently from "no calendar" —
*you may not see this* is not the same as *this does not exist*.

This is the conservative reading of Gap 4: access granted at team setup, not implied by
joining a session. The requirements flag the blanket-access model as needing Ron/Deepak
sign-off, and this stays on the safe side of that line.

---

## 11. Failure, and how it is reported

**A near-miss is a 200, not an error.** `no_capacity`, `no_block_large_enough`,
`slot_not_found`, `exceeds_capacity` all come back with a `status` and a plain-language
`message`, because a person needs to see *why* — not a stack trace.

**400 is reserved for a malformed request**: missing `transcript`, no `participants`,
`minutes` not a number, `day_offset` out of range. Validation happens before the model is
called, so a bad body never costs a token.

**A failed extraction returns 200** with `status: "failed"` and the reason in `errors[]`,
so a client can tell a model failure from a transport failure. **Check `status`, not just
the HTTP code.**

**A session failure never costs an extraction.** If memory cannot be loaded or saved, Otto
degrades to stateless and says so in `errors[]`.

---

## 12. Running it

```bash
cd otto
python -m otto --port 8100          # or: uvicorn otto.api:app --port 8100
```

| | |
|---|---|
| console | `http://localhost:8100/otto/test-ui` |
| health | `http://localhost:8100/health` |
| docs | `http://localhost:8100/docs` |

Settings come from the environment, a `.env` beside the package, or `rag/.env` — whichever
is found first. `/health` reports what is missing rather than failing opaquely.

### Free and repeatable

Pass a recorded `extraction` in the body and the model is never called. The deterministic
half runs exactly as it would live, which makes it the right way to develop a UI or
reproduce a bug.

---

## 13. Keeping this in sync

The package is **generated** from `rag/` by walking the AST:

```bash
python build_otto_package.py
```

That extracts each function by name from `rag/utils.py`, `rag/prompts.py` and
`rag/tools/story_point_calendar.py` and writes the modules here. The logic is production's,
not a copy that drifts. Re-run it after changing Otto in `rag/`.

The exceptions — written here, not extracted — are `api.py` (the endpoints, because the
originals are tangled with the main app's wiring), `llm.py`, `config.py` and
`__main__.py`. If `rag/main.py` changes an endpoint body, update `api.py` to match.

---

## 14. What Otto does not do

- **Schedule.** It proposes; a person confirms.
- **Estimate a duration.** No spoken size means `needs_duration`.
- **Remember across meetings.** 24-hour TTL, by design.
- **Create meetings.** `block_type: "team_meeting"` exists on the calendar but is fixed
  (Friday 10:00, 80 min) and generated by the packer. A task Otto places is a `task`, so
  asking Otto to "schedule a meeting" produces a task block, not a meeting.
- **Move or delete** a task it already placed. `placed_by: "otto_meeting"` marks them, so a
  future tool can find them — but that tool does not exist yet.
