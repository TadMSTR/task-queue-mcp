# task-queue-mcp

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes the agent orchestration task queue as an MCP tool interface. Agents submit tasks, check status, and record completions through typed, validated tools instead of raw YAML file writes.

Runs as a Docker container on port 8485. Wired globally into `~/.claude.json` so all Claude Code agent sessions have access.

## Tools

| Tool | Description |
|------|-------------|
| `submit_task` | Create a new task with `status: submitted` |
| `list_tasks` | List tasks with optional filters; TTL-expired tasks excluded |
| `get_task` | Retrieve a single task by UUID |
| `update_task` | Transition status and append a history entry |

### submit_task

```python
submit_task(
    source_agent="research",
    target_agent="claudebox",       # agent name or "auto" for dispatcher routing
    task_type="build",              # build | deploy | fix | research | review | audit | notify
    summary="Deploy qmd update",
    description="Apply the qmd stack update from build plan...",
    risk_level="low",               # low | medium | high (default: low)
    requires_approval=False,        # explicit override of approval gate
    priority="normal",              # normal | high | urgent (default: normal)
    context_refs=["/home/ted/.claude/projects/research/build-plans/qmd/plan.md"],
    ttl_days=30,
    workflow_mode="semi-auto",          # semi-auto | auto (default: semi-auto)
)
# → {"ok": true, "task_id": "<uuid>", "filename": "<timestamp>-<slug>.yml"}
```

`context_refs` must be absolute paths. `risk_level` and `priority` are validated against allowlists. `workflow_mode` controls dispatcher behavior: `semi-auto` (default) queues the task for operator pickup with a Matrix notification, while `auto` triggers the dispatcher to launch the target agent headlessly. The server generates the UUID, sets `created`, and initializes `alert_state` and `retry_policy` stubs.

### list_tasks

```python
list_tasks(
    target_agent="claudebox",       # optional
    source_agent="research",        # optional
    status="approved,in-progress",  # comma-separated, optional
    task_type="build",              # optional
    include_archived=False,         # include archive/ subdirectory
    limit=20,                       # max 200
)
# → list of task dicts, sorted by created descending
```

Tasks past their `ttl_days` are excluded. The dispatcher is authoritative for TTL archiving, but `list_tasks` filters them out proactively so agents don't act on stale items.

### get_task

```python
get_task(task_id="a7f3d2c1-1234-5678-abcd-000000000000")
# → full task dict, or {"ok": false, "error": "not found"}
```

Searches the main queue first, then `archive/`. Requires a full UUID — no prefix matching.

### update_task

```python
update_task(
    task_id="a7f3d2c1-1234-5678-abcd-000000000000",
    status="in-progress",           # see transition table below
    actor="claudebox",
    note="Claimed task, starting build.",
    output=None,                    # written to result.output on completed/failed
)
# → {"ok": true, "task_id": "<uuid>"} or {"ok": false, "error": "..."}
```

**Valid transitions:**

| From | To |
|------|----|
| `approved` | `in-progress` |
| `in-progress` | `completed` |
| Any non-terminal | `failed` |

Non-terminal: `submitted`, `pending-approval`, `approved`, `in-progress`.
Terminal: `completed`, `failed`.

`alert_state` and `retry_policy` are dispatcher-owned — `update_task` never touches them.

## Status Lifecycle

```
submitted → [pending-approval] → approved → in-progress → completed
                                                 ↓
                                              failed
```

The dispatcher owns the `submitted → approved/pending-approval` transitions. Agents own `approved → in-progress → completed` (or `failed`). Approval gating is controlled by agent manifests and the `requires_approval` field.

## Deployment

### Docker (production)

```yaml
services:
  task-queue-mcp:
    image: task-queue-mcp:latest
    container_name: task-queue-mcp
    ports:
      - "8485:8485"
    volumes:
      - /home/ted/.claude/task-queue:/task-queue
    environment:
      - TASK_QUEUE_DIR=/task-queue
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8485
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: [/tmp]
    user: "1000:1000"
    restart: unless-stopped
    networks:
      - claudebox-net
```

The container mounts only the task-queue directory read-write. The rest of the filesystem is read-only. `/tmp` is a tmpfs for transient scratch space.

### Claude Code settings.json

```json
{
  "mcpServers": {
    "task-queue-mcp": {
      "type": "url",
      "url": "http://localhost:8485/mcp"
    }
  }
}
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TASK_QUEUE_DIR` | `/task-queue` | Path to the task queue directory inside the container |
| `MCP_HOST` | `0.0.0.0` | Bind host for the HTTP server |
| `MCP_PORT` | `8485` | Port for the HTTP server |

## Building

```bash
docker build -t task-queue-mcp:latest .
```

## Development

```bash
pip install -r requirements.txt

# Run tests
pytest

# Run server locally (against a local task-queue directory)
TASK_QUEUE_DIR=/home/ted/.claude/task-queue python -m src.server
```

The test suite (21 tests) covers all four tools including validation edge cases and adversarial YAML strings. All writes use `yaml.dump` — never string interpolation — to prevent YAML injection.

## Security

Port 8485 is unauthenticated. Access is limited to LAN only — the port is not proxied externally via SWAG and the host firewall blocks external access. The container runs as UID 1000 with `cap_drop: ALL` and `no-new-privileges`.

## Task File Schema

Tasks are YAML files in `~/.claude/task-queue/`, named `YYYYMMDD-HHMMSS-<uuid-prefix>.yml`. All writes are atomic (write to `.tmp`, then `os.rename()`). Per-task file locks via `fcntl.flock` prevent races between concurrent MCP calls and the dispatcher.

For the full schema and lifecycle documentation, see the [homelab-agent component doc](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/task-queue-mcp.md).

## Related

- [homelab-agent](https://github.com/TadMSTR/homelab-agent) — agent orchestration documentation
- [task-dispatcher](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/task-dispatcher.md) — the dispatcher that routes and gates tasks
