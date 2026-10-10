"""Otto — meeting transcript to proposed tasks, standalone.

    from otto import app            # the FastAPI app
    uvicorn otto.api:app --port 8100

The model only EXTRACTS what people committed to. Every number a person is shown —
minutes free, which week, whether it fits — is computed in Python from the seat's
calendar, so it can never promise room the capacity check would refuse.

Read ARCHITECTURE.md for how the pieces fit together.
"""

__version__ = "1.0.0"

from .api import app                                            # noqa: F401
from .core import (build_otto_task_proposals, load_otto_seats,   # noqa: F401
                   parse_otto_request, answer_otto_requests)
from .capacity import check_seat_availability                    # noqa: F401
from .placement import confirm_otto_placement, propose_otto_placement  # noqa: F401
from .session import load_otto_session, save_otto_session        # noqa: F401
