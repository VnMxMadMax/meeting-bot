"""Assemble the standalone Otto package from the rag/ source.

Otto's logic lives inside a 20k-line utils.py and an 18k-line main.py. Copying it by
hand would drift the moment the backend changes, so this extracts the exact functions by
walking the AST and writes them out as a package that runs on its own.

Re-run it after changing Otto in rag/ and the package follows.

    python build_otto_package.py
"""

import ast
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
RAG = os.path.join(os.path.dirname(HERE), "Ronald-Johnson-AI", "rag")
OUT = os.path.join(HERE, "otto")

UTILS = io.open(os.path.join(RAG, "utils.py"), encoding="utf-8").read()
PROMPTS = io.open(os.path.join(RAG, "prompts.py"), encoding="utf-8").read()
SPC = io.open(os.path.join(RAG, "tools", "story_point_calendar.py"), encoding="utf-8").read()
MAIN = io.open(os.path.join(RAG, "main.py"), encoding="utf-8").read()


# ── slicing helpers ──────────────────────────────────────────────────────────
def func_src(src, name):
    """One function's exact source, by AST line numbers — no regex guessing."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            lines = src.splitlines(keepends=True)
            start = node.lineno - 1
            # include the decorator lines and any comment block directly above
            while start > 0 and lines[start - 1].lstrip().startswith(("#", "@")):
                start -= 1
            return "".join(lines[start:node.end_lineno])
    raise KeyError("function not found: %s" % name)


def const_src(src, name):
    i = src.index("\n%s = " % name) + 1
    m = re.compile(r"^(?:[A-Z_]{2,}\s*=|def |class |#\s*─)", re.M).search(src, i + len(name) + 5)
    return src[i:m.start()].rstrip() + "\n" if m else src[i:].rstrip() + "\n"


def block(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j].rstrip() + "\n"


# ── what Otto is made of ─────────────────────────────────────────────────────
# Order matters only for readability; Python resolves names at call time.

SMALL = ["_f", "_i", "_load", "_plan_root", "_week_capacity_for"]

GRAPH = ["fetch_seat_occupancy", "fetch_seat_calendar_plan", "store_seat_calendar_batch",
         "fetch_seat_for_user"]

ACCESS = ["resolve_seat_accounts", "seat_account_for"]

CAPACITY = ["seat_availability_from_plan", "check_seat_availability"]

CORE = ["_otto_bad", "_otto_norm", "_otto_quote_found", "_otto_fmt_min",
        "_otto_flatten_transcript", "_otto_seat_context",
        # pure logic over a list of proposals — lives here so session.py (which imports
        # core) does not create a cycle
        "otto_session_candidates",
        "parse_otto_request", "load_otto_seats", "otto_participants_for_prompt",
        "resolve_otto_assignee", "_otto_week_asked_for",
        "build_otto_task_proposals", "answer_otto_requests"]

PLACE = ["_otto_hhmm_to_min", "_otto_min_to_hhmm", "_otto_placement_token",
         "_otto_read_token", "_otto_open_blocks", "_otto_pick_open_block",
         "propose_otto_placement", "confirm_otto_placement"]

SESSION = ["_otto_session_now", "_otto_title_key", "load_otto_session",
           "save_otto_session", "merge_otto_proposals", "record_otto_placement"]

CONSTS = ["OTTO_MAX_TRANSCRIPT_CHARS", "OTTO_MAX_PARTICIPANTS", "OTTO_MAX_SEATS",
          "OTTO_PLACEMENT_TOKEN_VERSION", "OTTO_MIN_PLACEMENT_MINUTES",
          "OTTO_PLACEMENT_SOURCE", "OTTO_SESSION_TTL_HOURS", "_OTTO_WEEKDAYS"]


def gather(names):
    return "\n\n".join(func_src(UTILS, n).rstrip() for n in names) + "\n"


def write(rel, text):
    path = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8").write(text)
    print("  %-28s %5d lines" % (rel, text.count("\n")))


os.makedirs(OUT, exist_ok=True)
print("writing otto/ package from rag/ source\n")

# ── otto/config.py ───────────────────────────────────────────────────────────
write("config.py", '''"""Credentials and the one Neo4j driver Otto uses.

Read from the environment, or a .env beside this package, or rag/.env — whichever is
found first. Nothing is hard-coded, so this package carries no secrets.
"""

import logging
import os

logger = logging.getLogger("otto")

try:
    from dotenv import load_dotenv
    for _candidate in (
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "Ronald-Johnson-AI", "rag", ".env"),
        ".env",
    ):
        if os.path.isfile(_candidate):
            load_dotenv(_candidate)
            break
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NEO4J_URI      = os.getenv("NEO4J_URI", "")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM  = os.getenv("JWT_ALGORITHM", "HS256")

OPENAI_MODEL = os.getenv("OTTO_MODEL", "gpt-4o")
OPENAI_TEMPERATURE = 0.0          # extraction must be repeatable


def create_driver():
    """A Neo4j driver with the API's own connection settings.

    Aura recycles idle connections, so the lifetime is kept under the server-side
    timeout and the socket is kept alive — otherwise the pool hands out a half-dead
    connection.
    """
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
        connection_timeout=30,
        max_connection_lifetime=300,
        keep_alive=True,
        max_connection_pool_size=50,
        connection_acquisition_timeout=10,
    )


def missing_settings():
    """Which required settings are absent — so a caller can say so plainly."""
    return [name for name, value in [
        ("NEO4J_URI", NEO4J_URI), ("NEO4J_PASSWORD", NEO4J_PASSWORD)] if not value]
''')

# ── otto/prompt.py ───────────────────────────────────────────────────────────
prompt_block = block(PROMPTS, "OTTO_MEETING_TASKS_SYSTEM",
                     "otto_meeting_tasks_prompt = ChatPromptTemplate")
write("prompt.py", '''"""The extraction prompt, verbatim from rag/prompts.py.

The model does ONE job: read a transcript and say what was committed to and what was
asked. It never sees remaining capacity and never schedules — every number a person is
shown is computed in Python from the calendar. An LLM-written "you have about 20 hours"
is a figure nobody can check, and Otto must never promise room the capacity check would
then refuse.
"""

''' + prompt_block)

# ── otto/helpers.py ──────────────────────────────────────────────────────────
write("helpers.py", '''"""Small shared pieces: coercion, the weekly capacity table, plan unwrapping."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .config import logger

''' + const_src(SPC, "_WEEK_CAPACITY") + "\n\n" + gather(SMALL).replace(
    "from tools.story_point_calendar import _WEEK_CAPACITY\n", "") + '''

def derive_story_point(duration_minutes):
    """Duration -> Fibonacci story point. Verbatim from tools/story_point_calendar.py."""
    from .story_points import derive_story_point as _d
    return _d(duration_minutes)
''')

# ── otto/story_points.py ─────────────────────────────────────────────────────
fib = const_src(SPC, "_FIBONACCI_STORY_POINTS")
scale = const_src(SPC, "_FIBONACCI_SCALE")
write("story_points.py", '''"""Fibonacci story points, from tools/story_point_calendar.py.

A story point measures a whole ACTIVITY, not a sitting. That is why an activity's points
sit on its first sitting and every later sitting carries 0 — splitting 3 points over 8
sittings gives fractions that do not add back.
"""

''' + fib + "\n" + scale + "\n\n"
      + func_src(SPC, "_ceil_div") + "\n"
      + func_src(SPC, "activity_count") + "\n"
      + func_src(SPC, "derive_story_point"))

# ── otto/graph.py ────────────────────────────────────────────────────────────
write("graph.py", '''"""Every read and write Otto makes against Neo4j.

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

''' + gather(GRAPH))

# ── otto/access.py ───────────────────────────────────────────────────────────
write("access.py", '''"""Whose calendar may this caller read? (Gap 4)

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

''' + gather(ACCESS))

# ── otto/capacity.py ─────────────────────────────────────────────────────────
write("capacity.py", '''"""Does a task of size N fit on this seat, and if not, when? (Gap 2)

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

''' + gather(CAPACITY))

# ── otto/session.py ──────────────────────────────────────────────────────────
write("session.py", '''"""Short-term memory — ONE meeting, carried across calls.

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

''' + const_src(UTILS, "OTTO_SESSION_TTL_HOURS") + "\n" + gather(SESSION))

# ── otto/core.py ─────────────────────────────────────────────────────────────
consts = "\n".join(const_src(UTILS, c).rstrip() for c in
                   ["OTTO_MAX_TRANSCRIPT_CHARS", "OTTO_MAX_PARTICIPANTS",
                    "OTTO_MAX_SEATS", "_OTTO_WEEKDAYS"])
write("core.py", '''"""The deterministic half: validate, resolve, ground, decide, answer.

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

''' + consts + "\n\n" + gather(CORE))

# ── otto/placement.py ────────────────────────────────────────────────────────
write("placement.py", '''"""The write half: propose a slot, a person confirms, the calendar is updated.

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

''' + "\n".join(const_src(UTILS, c).rstrip() for c in
                ["OTTO_PLACEMENT_TOKEN_VERSION", "OTTO_MIN_PLACEMENT_MINUTES",
                 "OTTO_PLACEMENT_SOURCE"]) + "\n\n" + gather(PLACE))

print("\npackage written to %s" % OUT)
