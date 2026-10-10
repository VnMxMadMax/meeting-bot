# Otto — standalone

Meeting transcript → proposed tasks, checked against each person's calendar before
anything is booked.

This folder runs Otto on its own, outside the main API. **[ARCHITECTURE.md](ARCHITECTURE.md)
explains how it works** — read that to understand the system.

## Run it

```bash
pip install -r requirements.txt
python -m otto --port 8100
```

| | |
|---|---|
| console | http://localhost:8100/otto/test-ui |
| health | http://localhost:8100/health |
| API docs | http://localhost:8100/docs |

Settings are read from the environment, a `.env` beside this folder, or `rag/.env` —
whichever is found first. Nothing is hard-coded here.

```
NEO4J_URI=neo4j+s://…
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=…
JWT_SECRET_KEY=…          # to accept the app's tokens
OPENAI_API_KEY=sk-…       # only for a live extraction
```

`/health` says what is missing rather than failing opaquely.

## Endpoints

| | | |
|---|---|---|
| `POST` | `/otto/meeting-tasks` | **the main call** — transcript → proposals + answers |
| `POST` | `/otto/propose-placement` | where would this task go? **writes nothing** |
| `POST` | `/otto/confirm-placement` | the person said yes — **writes** |
| `GET` | `/seat-availability/{seat_id}` | does a task of size N fit? |
| `GET` | `/seat-assignments` | every seat and its occupant |
| `POST` | `/seat-lookup` | one person's seat: id, name, role, department |

## Use it as a library

```python
from otto import parse_otto_request, load_otto_seats, build_otto_task_proposals

parsed = parse_otto_request(body)
seats  = load_otto_seats(user_id, parsed["participants"], sprint_week=1)["seats"]
result = build_otto_task_proposals(extraction, parsed["transcript"],
                                   parsed["participants"], seats)
```

## Develop without spending tokens

Pass a recorded `extraction` in the request body and the model is never called — the
deterministic half runs exactly as it would live. Free, repeatable, and the right way to
build a UI or reproduce a bug.

## Regenerating

The modules are **extracted from `rag/` by AST**, so the logic is production's rather than
a copy that drifts:

```bash
python build_otto_package.py
```

Re-run it after changing Otto in `rag/`. Written by hand, not extracted: `api.py`,
`llm.py`, `config.py`, `__main__.py`.
