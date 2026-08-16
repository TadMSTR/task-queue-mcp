# task-queue-mcp

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes the agent orchestration task queue as an MCP tool interface. Agents submit tasks, check status, and record completions through typed, validated tools instead of raw YAML file writes.

Runs as a Docker container on port 8485. Wired globally into `~/.claude.json` so all Claude Code agent sessions have access.

## Tools

| Tool | Description |
|------|-------------|
| `submit_task` | Create a new task with `status: submitted` |
| `list_tasks` | List tasks with optional filters; TTL-expired tasks excluded |
| `get_task` | Retrieve a single task by UUID (resolves archived tasks too) |
| `update_task` | Agent-facing status transition (strict); appends a history entry |
| `set_task_status` | Operator status change — approve, cancel, park, or advance a missed task (audited override) |
| `cancel_task` | Graceful terminal `cancelled` state for stale tasks (record kept, never deleted) |
| `park_task` | Pause a task without hiding it — stays listed, exempt from TTL, nothing picks it up |
| `unpark_task` | Return a parked task to the status it was parked from |
| `amend_task` | Append a correction to a queued task; the original description is never rewritten |

Agents use the strict `update_task` path; operators (via the HTTP control API) use
`set_task_status` / `cancel_task` / `park_task` / `unpark_task`. Agents cannot cancel or
park — both are operator-only. `amend_task` is the exception: the task's *source* agent may
amend it, but the target agent may not.

### submit_task

```python
submit_task(
    source_agent="research",
    target_agent="deploy-agent",  # agent name or "auto" for dispatcher routing
    # build | deploy | fix | research | review | audit | notify | docs |
    # ticket_audit | ticket_audit_complete
    task_type="build",
    summary="Deploy qmd update",
    description="Apply the qmd stack update from build plan...",
    risk_level="low",  # low | medium | high (default: low)
    requires_approval=False,  # explicit override of approval gate
    priority="normal",  # normal | high | urgent (default: normal)
    context_refs=["/srv/agents/build-plans/qmd/plan.md"],  # absolute paths only
    ttl_days=30,
    workflow_mode="semi-auto",  # semi-auto | auto (default: semi-auto)
    originating_task_id=None,  # UUID of the parent task, if this is a return task
)
# → {"ok": true, "task_id": "<uuid>", "filename": "<timestamp>-<slug>.yml"}
```

`context_refs` must be absolute paths. `risk_level` and `priority` are validated against allowlists. `workflow_mode` controls dispatcher behavior: `semi-auto` (default) queues the task for operator pickup with a Matrix notification, while `auto` triggers the dispatcher to launch the target agent headlessly. The server generates the UUID, sets `created`, and initializes the `retry_policy` stub.

#### Auto-close of the originating task (since v0.6.0)

Pass `originating_task_id` and the parent is closed as `completed` — **submitting the return task is what closes the request.** The response gains `auto_closed_task_id` when it fires.

It fires only if all of these hold:

| Condition | Why |
|---|---|
| the parent resolves, and is not archived | nothing to close otherwise |
| `parent.target_agent == source_agent` | **the bound on the whole feature** — agent A must not be able to close agent B's task by naming it as a parent. Checked here explicitly rather than relying on `update_task`'s ownership check, which also admits `operator` |
| parent is at `approved` or `in-progress` | `parked` is an operator's deliberate pause; `submitted`/`pending-approval` are not approved yet; `routing-failed` is still being retried by the dispatcher |

An `approved` parent is walked through `in-progress` first, so its history reads as claimed-then-closed rather than teleported.

This is a **fail-safe, not the primary path**. Agents are still expected to close their own tasks explicitly — that puts the agent's own note in the history, where this writes only `auto-closed: return task <id> submitted`. Any failure inside the auto-close is logged at warning level and the submit returns normally; it can never fail the submit it is a side effect of.

### list_tasks

```python
list_tasks(
    target_agent="deploy-agent",  # optional
    source_agent="research",  # optional
    status="approved,in-progress",  # comma-separated, optional
    task_type="build",  # optional
    include_archived=False,  # include archive/ subdirectory
    limit=20,  # max 200
)
# → list of task dicts, sorted by created descending
```

**An unrecognised `status` is an error, not an empty result (since v0.6.0).** It used to be filtered on silently, which is how a sweep for `status="pending"` — never a status here — returned `[]` for months, indistinguishable from "no work for you". An empty list is a legitimate answer to a well-formed question, so the only way to tell a typo apart from an empty queue is to refuse the typo. Whitespace and a trailing comma are still tolerated; an empty string still means no filter.

Tasks past their `ttl_days` are excluded. The dispatcher is authoritative for TTL archiving, but `list_tasks` filters them out proactively so agents don't act on stale items.

**Parked tasks are exempt from the TTL filter.** Parking is a deliberate "pause this, I'll come back to it" — a parked task quietly expiring out of the listing would defeat the point of the status.

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
    status="in-progress",  # see transition table below
    actor="deploy-agent",
    note="Claimed task, starting build.",
    output=None,  # written to result.output on completed/failed
)
# → {"ok": true, "task_id": "<uuid>"} or {"ok": false, "error": "..."}
```

**Ownership check (since v0.5.0):** `actor` must equal the task's `target_agent`, or be
`"operator"` — any other actor is rejected. This closes the gap where an agent other than
the one a task was assigned to could claim or complete it.

**Valid transitions:**

| From | To |
|------|----|
| `approved` | `in-progress` |
| `in-progress` | `completed` |
| Any non-terminal | `failed` |

Non-terminal: `submitted`, `pending-approval`, `approved`, `in-progress`, `parked`, `routing-failed`.
Terminal: `completed`, `failed`, `cancelled`.

`routing-failed` is dispatcher-written and deliberately excluded from the `Any non-terminal → failed`
row above — an agent must not be able to terminally fail a task the dispatcher is still retrying. It
is a normal source for the operator transitions below (`cancelled`, `parked`, override).

`retry_policy` is dispatcher-owned — `update_task` never touches it.

### Operator transitions (`set_task_status`)

Broader than `update_task` but still audited and bounded:

| From | To | Notes |
|------|----|-------|
| `submitted` / `pending-approval` | `approved` | standard |
| Any non-terminal | `cancelled` | standard (also via `cancel_task`) |
| Any non-terminal | `parked` | standard (also via `park_task`) |
| Any non-terminal | Any non-terminal | requires `allow_override=True` + a non-empty note (the "advance a missed task" override) |
| Any *unrecognised* status | Any valid status | requires `allow_override=True` + a non-empty note (the repair path) |

Terminal tasks are immutable even for operators. Every operator change appends a history entry with `actor` + `note`.

**The repair path** exists because the queue directory has more than one writer. A record whose status is outside this server's vocabulary entirely — a historic `complete` typo, or a future dispatcher status not yet admitted here — is unreachable by every other branch and would otherwise be permanently stuck. Repair only ever moves a task *out of* an invalid status; the target must still be valid, and the history entry records `repaired_from`. `routing-failed` no longer needs this path — it's a first-class non-terminal status now (see above), reachable via the standard `cancelled`/`parked` rows or the plain override row.

### park_task / unpark_task

```python
park_task(task_id="...", actor="operator", note="waiting on upstream fix")
# → {"ok": true, "task_id": "<uuid>"}

unpark_task(task_id="...", actor="operator", status=None)
# → returns the task to the status it was parked from
```

Parking changes only the status — the YAML never moves. The task keeps appearing in `list_tasks`, is exempt from TTL expiry, and nothing picks it up, because the dispatcher's pickup loops match `submitted` and `routing-failed` only. The prior status is recorded in `parked_from` and cleared on the way out, so it can never go stale. Pass `status` to `unpark_task` to send the task somewhere other than where it came from — required for a task parked by a direct-YAML writer, which carries no `parked_from`.

Park is for "not now, but don't lose this". A long-idle task is not necessarily neglect, and `parked` is the vocabulary that distinguishes a deliberate bookmark from something genuinely abandoned.

### amend_task

```python
amend_task(
    task_id="...",
    amendment="Preflight answered the open question — FastMCP mount() is live-linked.",
    actor="research",  # the task's source_agent, or "operator"
    reason="preflight ran after queuing",
)
# → {"ok": true, "task_id": "...", "amendment_count": 1, "agent_may_have_started": false}
```

Once a task is queued its description is immutable. When something changes between queuing and starting — a preflight answers an open question, a dependency lands, a reviewer spots an error, scope narrows — the correction has nowhere to go, and an agent that trusts its task description will do the wrong thing.

`amend_task` closes that gap **append-only**. `payload.description` is never mutated; amendments accumulate under `payload.amendments` as `{timestamp, actor, reason, text}` and readers render them after the description. What the task originally asked for stays on the record.

| Rule | Behaviour |
|---|---|
| Who may amend | The task's `source_agent`, or `operator`. **The target agent is rejected** — it must not rewrite the instructions it was handed, the same trust boundary that makes `cancelled` operator-only. |
| When | Any non-terminal task, including `in-progress` and `parked`. Terminal and archived tasks are rejected. |
| in-progress | Permitted — it is the case that matters most — but the response sets `agent_may_have_started: true`, since the agent may already have read the original. Tell it out of band. |
| Bounds | 10 amendments per task, 4096 chars each. |

**Scope-creep guideline:** more than one or two amendments on a task is a signal to cancel and re-queue rather than accrete. The bounds are a backstop, not a budget.

## Status Lifecycle

```
submitted → [pending-approval] → approved → in-progress → completed
                                                 ↓
                                              failed

routing-failed  # dispatcher-written on a failed dispatch attempt; non-terminal

Any non-terminal ──(operator)──> cancelled     # graceful dismissal, record kept
Any non-terminal <──(operator)──> parked       # pause; stays listed, TTL-exempt
```

The dispatcher owns the `submitted → approved/pending-approval` transitions, and also writes
`routing-failed` when a dispatch attempt fails (it retries on its own schedule; operators can
also cancel, park, or force it elsewhere via `set_task_status`). Agents own `approved →
in-progress → completed` (or `failed`) — `routing-failed` is not reachable through `update_task`.
Operators own `cancelled`, `parked`, and audited status overrides. Approval gating is controlled
by agent manifests and the `requires_approval` field.

**Every task is closed by its own target agent.** That follows from `update_task`'s ownership
check, and it is the one rule to keep in mind when wiring a new cross-agent workflow: the agent
that submits a request cannot close it, because the request targets someone else. A request/return
pair therefore needs the *receiving* agent to claim and close its own entry — two calls, since
`completed` is only reachable from `in-progress`. The [auto-close](#auto-close-of-the-originating-task-since-v060)
is the fail-safe for when it doesn't, not a substitute for it.

## HTTP Control API

Non-MCP clients (the CloudCLI plugin and Matrix bot) can't import the Python core, so all their mutations go through a thin HTTP control API mounted as FastMCP custom routes on the **same port 8485**. Each endpoint delegates to the tool handlers above, inheriting transition validation, `fcntl` locking, and atomic writes — so there is exactly one validated write path for the whole system.

| Method | Path | Delegates to |
|--------|------|--------------|
| `POST` | `/tasks/{id}/approve` | `set_task_status(approved)` |
| `POST` | `/tasks/{id}/cancel` | `cancel_task` |
| `POST` | `/tasks/{id}/status` | `set_task_status` (body: `status`, `note`, `allow_override`) |
| `POST` | `/tasks/{id}/park` | `park_task` |
| `POST` | `/tasks/{id}/unpark` | `unpark_task` (body: optional `status`) |
| `POST` | `/tasks/{id}/amend` | `amend_task` (body: `amendment`, optional `reason`) |
| `GET` | `/queue/summary` | Counts by status across the active queue |

Body fields: `actor` (default `operator`), `note`, plus `status` / `allow_override` for the status route, `amendment` / `reason` for amend. Responses map the canonical result: `200` ok, `404` not found, `400` validation/transition error.

`GET /queue/summary` returns `{"ok": true, "counts": {...}, "active": N, "total": N}`, where `active` is the non-terminal total (now including `routing-failed`, counted by name). Statuses outside the server's vocabulary entirely are bucketed under `"unknown"` rather than dropped, so records written by other direct-YAML writers stay visible in the count.

**Auth:** custom routes bypass the MCP auth middleware, so a shared-secret header is the gate (defense in depth on top of the loopback-published port):

- Send `X-Task-Queue-Secret: $TASK_QUEUE_API_SECRET` on every mutation.
- The server compares it in constant time (`hmac.compare_digest`) and **fails closed** (401) when the secret is missing, wrong, or unconfigured.
- The secret lives in an operator-managed env file outside the repo, injected via `env_file` into the container and into each client's environment — never committed to source.

## Deployment

### Docker (production)

```yaml
services:
  task-queue-mcp:
    image: task-queue-mcp:latest
    container_name: task-queue-mcp
    ports:
      # The loopback bind is load-bearing, not cosmetic. The MCP transport on this port
      # is unauthenticated (see Trust model below), so publishing it as "8485:8485"
      # would expose an unauthenticated queue-mutation endpoint to your whole LAN.
      - "127.0.0.1:8485:8485"
    volumes:
      - ~/.claude/task-queue:/task-queue   # host queue directory
    environment:
      - TASK_QUEUE_DIR=/task-queue
      # 0.0.0.0 here is the *container-internal* bind and must stay wide, or the port
      # mapping above has nothing to forward to. The host-side bind is what limits reach.
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8485
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    read_only: true
    tmpfs: [/tmp]
    user: "1000:1000"
    restart: unless-stopped
    networks:
      - agent-net
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
| `TASK_QUEUE_API_SECRET` | — | Shared secret for the HTTP control API. **Required** for any control-API mutation — fails closed (401) if unset. The MCP tools themselves do not use it. |

## Building

```bash
docker build -t task-queue-mcp:latest .
```

## Development

Requires Python 3.11+.

```bash
pip install -e ".[dev]"

# Lint + format (Baseline gate)
ruff check .
ruff format --check .

# Tests with coverage (gate: >=80%)
python -m pytest --cov=src --cov-report=term-missing

# Run server locally against a local task-queue directory
TASK_QUEUE_DIR=~/.claude/task-queue python -m src.server
```

The test suite covers every tool and the HTTP control API — validation edge cases, adversarial YAML strings, illegal transitions, the park/unpark round-trip, `amend_task` authorization (including the rejected target agent), operator-override auditing, out-of-vocabulary status repair, and the shared-secret gate (missing/wrong secret → 401). All writes use `yaml.dump` — never string interpolation — to prevent YAML injection.

## Security

The MCP tool endpoint on port 8485 is unauthenticated and limited to LAN/loopback — the port is not proxied externally via SWAG and the host firewall blocks external access. The **HTTP control API** mutation routes additionally require a shared-secret header (`X-Task-Queue-Secret`, constant-time compare, fail-closed) — see [HTTP Control API](#http-control-api). The container runs as UID 1000 with `cap_drop: ALL`, `no-new-privileges`, and a read-only rootfs (only `/task-queue` is writable).

### Trust model

**Loopback is the trust boundary.** The shared secret gates only the cross-process HTTP control routes (`/tasks/...`) — it is *not* the sole barrier to mutation. All MCP tools, including the operator-mutating `set_task_status` / `cancel_task` / `park_task` / `unpark_task` and the content-mutating `amend_task`, are reachable via the unauthenticated `/mcp/` JSON-RPC endpoint, so **any process with loopback access to port 8485 can mutate the queue without the secret.** In particular, `amend_task`'s source-agent authorization is an *integrity* control over a self-asserted `actor`, not an authentication one — it stops an agent from casually rewriting its own brief and keeps the amendment attributable in history; it does not stop a loopback process from claiming any actor it likes. This is intentional: the queue is internal agent-coordination state, the port is loopback-only, and the MCP transport has always been unauthenticated. The secret exists to authenticate the *specific* cross-process clients (the CloudCLI plugin and Matrix bot) over plain HTTP, not to harden the loopback boundary. If loopback trust ever becomes insufficient, gate the MCP transport with a FastMCP auth provider rather than relying on the control-route secret alone.

## Task File Schema

Tasks are YAML files in `~/.claude/task-queue/`, named `YYYYMMDD-HHMMSS-<uuid-prefix>.yml`. All writes are atomic (write to `.tmp`, then `os.rename()`). Per-task file locks via `fcntl.flock` prevent races between concurrent MCP calls and the dispatcher.

For the full schema and lifecycle documentation, see the [homelab-agent component doc](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/task-queue-mcp.md).

## Related

- [homelab-agent](https://github.com/TadMSTR/homelab-agent) — agent orchestration documentation
- [task-dispatcher](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/task-dispatcher.md) — the dispatcher that routes and gates tasks
