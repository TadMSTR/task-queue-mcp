# task-queue-mcp

FastMCP server backed by YAML files on disk. Cross-agent task dispatch for the forge platform. Runs in a Docker container with the queue directory mounted.

## What it does

Provides 4 tools for submitting, listing, retrieving, and updating task files. Each task is a single YAML file in `TASK_QUEUE_DIR`.

## Tools

- `submit_task(source_agent, target_agent, task_type, summary, description, ...)` — Write a YAML task file to `TASK_QUEUE_DIR`. Returns `{ok, task_id, filename}`.
- `list_tasks(target_agent, source_agent, status, limit)` — Filter tasks from the queue directory.
- `get_task(task_id)` — Retrieve a single task by ID.
- `update_task(task_id, status, ...)` — Update task status: `pending` → `approved` → `in-progress` → `complete`.

## Structure

```
src/
  server.py         FastMCP server — 4 tools, lifespan checks TASK_QUEUE_DIR exists
  tools/
    queue.py        submit_task_handler, list_tasks_handler, get_task_handler,
                    update_task_handler — all file I/O on TASK_QUEUE_DIR
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

- **YAML files as the queue** — no database. Each task is one file named `<task_id>.yaml`. The queue is human-inspectable with standard tools and survives container restarts without any migration.
- **No in-process state** — all operations read/write files directly. Multiple server instances can safely share a queue directory via NFS or bind mount.
- **Fail-fast on missing dir** — the server exits at startup if `TASK_QUEUE_DIR` does not exist. This prevents tasks from being silently dropped due to a missing volume mount.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
