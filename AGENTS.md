# task-queue-mcp

FastMCP server backed by YAML files on disk. Cross-agent task dispatch for the forge platform. Runs in a Docker container with the queue directory mounted.

## What it does

Provides 9 tools for submitting, listing, retrieving, and mutating task files. Each task is a single YAML file (`<timestamp>-<slug>.yml`) in `TASK_QUEUE_DIR`.

Task status lifecycle:

```
submitted ──▶ approved ──────────▶ in-progress ──▶ completed
     │                                              └─▶ failed
     └──▶ pending-approval ──▶ approved            cancelled (terminal, any non-terminal state)
                                                    parked         (non-terminal, reversible)
                                                    routing-failed (non-terminal, dispatcher-written;
                                                                    operator can cancel/park/re-submit it,
                                                                    agents cannot reach it via update_task)

submitted ══▶ completed   (task_type=notify only — a creation path, not a transition;
                            written directly by submit_task, never via update_task.
                            VALID_TRANSITIONS["completed"] is still exactly {"in-progress"}.)
```

Terminal statuses: `completed`, `failed`, `cancelled`. Transitions are validated server-side; agents cannot claim (`in-progress`) a task that is not `approved`. The one exception is by construction, not transition: a `notify` task is written straight to `completed` at submit time and never passes through `approved` or `in-progress` at all.

## Tools

- `submit_task(source_agent, target_agent, task_type, summary, description, ...)` — Write a YAML task file to `TASK_QUEUE_DIR` (initial status `submitted`, except `task_type="notify"`, which is written straight to `completed` — see the lifecycle diagram above). Returns `{ok, task_id, filename}`.
- `list_tasks(target_agent, source_agent, status, limit)` — Filter tasks from the queue directory.
- `get_task(task_id)` — Retrieve a single task by ID.
- `update_task(task_id, status, ...)` — Move a task to `in-progress`, `completed`, or `failed` (records actor/note/history).
- `set_task_status(task_id, ...)` — Operator-lane transitions (`approved`, `cancelled`, `parked`, audited overrides, out-of-vocabulary repair). `routing-failed` is a valid non-terminal source here, same as any other — an operator can cancel, park, or re-submit a task stuck in it.
- `cancel_task(task_id, actor, note)` — Cancel a non-terminal task.
- `park_task(task_id, actor, note)` — Pause a task in place. Status-only; the file never moves.
- `unpark_task(task_id, actor, note, status)` — Return a parked task to `parked_from`, or to an explicit status.
- `amend_task(task_id, amendment, actor, reason)` — Append a correction under `payload.amendments`. Never mutates `payload.description`.

## Structure

```
src/
  server.py         FastMCP server — 9 tools + the HTTP control API custom routes,
                    lifespan checks TASK_QUEUE_DIR exists
  tools/
    queue.py        submit/list/get/update/set_task_status/cancel/park/unpark/
                    amend _handler — all file I/O on TASK_QUEUE_DIR
tests/              pytest tests
Dockerfile          Container image — TASK_QUEUE_DIR must be a mounted volume
pyproject.toml
requirements.txt
```

## Configuration

| Env var           | Default        | Purpose                                          |
|-------------------|----------------|--------------------------------------------------|
| `TASK_QUEUE_DIR`  | `/task-queue`  | Directory for task YAML files (mount a volume)   |

## Architecture decisions

- **YAML files as the queue** — no database. Each task is one file named `<timestamp>-<slug>.yml`. The queue is human-inspectable with standard tools and survives container restarts without any migration.
- **No in-process state** — all operations read/write files directly. Multiple server instances can safely share a queue directory via NFS or bind mount.
- **Fail-fast on missing dir** — the server exits at startup if `TASK_QUEUE_DIR` does not exist. This prevents tasks from being silently dropped due to a missing volume mount.
- **Park is a status, not a directory move** — an earlier `quarantine` mechanism relocated the YAML into a subdirectory. Readers that only listed the main queue therefore lost sight of the task entirely, which is the opposite of what pausing is for. `parked` keeps the file exactly where it is, so every existing reader picks it up with no change.
- **Amendments are append-only** — `payload.description` is write-once at submit. Corrections accumulate under `payload.amendments`; readers render description then amendments in order. The record of what was originally asked always survives.
- **The queue has more than one writer.** This server is not the only process writing task YAML — a dispatcher does too. It writes `routing-failed`, which is now a first-class member of `VALID_STATUSES` (vikunja#324, 2026-08) precisely so an operator can act on it directly, rather than only through `set_task_status`'s override/repair path. It is deliberately excluded from `VALID_TRANSITIONS["failed"]` — an agent must not be able to terminally fail a task the dispatcher is still retrying. A writer can still land a genuinely unrecognised status outside the vocabulary entirely (a typo, a future dispatcher status not yet admitted here); `list_tasks` must tolerate that, `/queue/summary` buckets it under `unknown`, and `set_task_status` keeps its repair path for moving out of one. Widening `VALID_STATUSES` narrows that surface but does not eliminate the need to tolerate the unknown.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
