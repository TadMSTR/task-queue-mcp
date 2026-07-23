# task-queue-mcp

FastMCP server backed by YAML files on disk. Cross-agent task dispatch for the forge platform. Runs in a Docker container with the queue directory mounted.

## What it does

Provides 8 tools for submitting, listing, retrieving, and mutating task files. Each task is a single YAML file (`<timestamp>-<slug>.yml`) in `TASK_QUEUE_DIR`.

Task status lifecycle:

```
submitted ──▶ approved ──────────▶ in-progress ──▶ completed
     │                                              └─▶ failed
     └──▶ pending-approval ──▶ approved            cancelled (terminal, any non-terminal state)
```

Terminal statuses: `completed`, `failed`, `cancelled`. Transitions are validated server-side; agents cannot claim (`in-progress`) a task that is not `approved`.

## Tools

- `submit_task(source_agent, target_agent, task_type, summary, description, ...)` — Write a YAML task file to `TASK_QUEUE_DIR` (initial status `submitted`). Returns `{ok, task_id, filename}`.
- `list_tasks(target_agent, source_agent, status, limit)` — Filter tasks from the queue directory.
- `get_task(task_id)` — Retrieve a single task by ID.
- `update_task(task_id, status, ...)` — Move a task to `in-progress`, `completed`, or `failed` (records actor/note/history).
- `set_task_status(task_id, ...)` — Approval-lane transitions (e.g. `approved`).
- `cancel_task(task_id, actor, note)` — Cancel a non-terminal task.
- `quarantine_task(task_id, actor, note)` — Move a task into the `quarantine/` subdir.
- `restore_task(task_id, actor, note)` — Restore a quarantined task to the queue.

## Structure

```
src/
  server.py         FastMCP server — 8 tools, lifespan checks TASK_QUEUE_DIR exists
  tools/
    queue.py        submit/list/get/update/set_task_status/cancel/quarantine/
                    restore _handler — all file I/O on TASK_QUEUE_DIR
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

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
