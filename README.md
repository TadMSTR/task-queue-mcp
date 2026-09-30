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
| `get_task` | Retrieve a single task by UUID (resolves archived and dead-lettered tasks too) |
| `update_task` | Agent-facing status transition (strict); appends a history entry |
| `set_task_status` | Operator status change — approve, cancel, park, or advance a missed task (audited override) |
| `cancel_task` | Graceful terminal `cancelled` state for stale tasks (record kept, never deleted) |
| `park_task` | Pause a task without hiding it — stays listed, exempt from TTL, nothing picks it up |
| `unpark_task` | Return a parked task to the status it was parked from |
| `amend_task` | Append a correction to a queued task; the original description is never rewritten |
| `requeue_dead_letter` | Operator-only — return a dead-lettered task to the queue at `submitted` |

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
    workflow_mode="semi-auto",  # semi-auto | auto | manual-then-auto (default: semi-auto)
    originating_task_id=None,  # UUID of the parent task, if this is a return task
)
# → {"ok": true, "task_id": "<uuid>", "filename": "<timestamp>-<slug>.yml"}
```

`context_refs` must be absolute paths. `risk_level` and `priority` are validated against allowlists. `workflow_mode` controls dispatcher behavior: `semi-auto` (default) queues the task for operator pickup with a Matrix notification, while `auto` triggers the dispatcher to launch the target agent headlessly. `manual-then-auto` gates only its own leg — this task waits for an operator Start exactly like `semi-auto`, but every task the resulting session spawns inherits `auto`. The mode is stored verbatim on the parent task; the downgrade to `auto` for children happens in the dispatcher, not here, so a reader inspecting the stored YAML sees `manual-then-auto` unchanged. The server generates the UUID, sets `created`, and initializes the `retry_policy` stub.

#### `notify` is self-terminal

`submit_task` writes a `notify` task straight to `completed` — `result.output` carries the description, `result.completed_by` is `<source_agent> (notify)`, no agent session is ever launched, and nobody has to close it. `requires_approval` is forced `False` for the type.

Use `notify` for "here is a result, nothing to do" — a verdict, an outcome, an FYI. Do **not** use it to ask for work: a `notify` task is never `approved`, so it never appears in the `list_tasks(status="approved")` sweep agents use as a work list, and a request filed that way vanishes without an error. It remains visible to an unfiltered `list_tasks` until its TTL — readable, not assignable.

Submitting a `notify` still auto-closes the request it answers via `originating_task_id`, exactly as any other return task does — see [Auto-close](#auto-close-of-the-originating-task-since-v060) below.

#### Auto-close of the originating task (since v0.6.0)

Pass `originating_task_id` and the parent is closed as `completed` — **submitting the return task is what closes the request.** The response gains `auto_closed_task_id` when it fires.

It fires only if all of these hold:

| Condition | Why |
|---|---|
| the parent resolves, and is not archived | nothing to close otherwise |
| `parent.target_agent == source_agent` | **the bound on the whole feature** — agent A must not be able to close agent B's task by naming it as a parent. Checked here explicitly rather than relying on `update_task`'s ownership check, which also admits `operator` |
| `parent.source_agent == target_agent` | the other half of the **return shape** — you must be answering whoever asked. Without it a *forward* request looks identical to a return (see below) |
| parent is at `approved` or `in-progress` | `parked` is an operator's deliberate pause; `submitted`/`pending-approval` are not approved yet; `routing-failed` is still being retried by the dispatcher |

**Why both halves (since v0.6.1).** `originating_task_id` is overloaded: on a return task it means "this answers that request", but on a *forward* request it means "inherit `workflow_mode` from this parent" — which is what a build agent passes when it files an audit request for its own in-flight build. Checking only the first condition cannot tell those apart, because the build task targets the build agent and the build agent is the submitter. v0.6.0 shipped with only the first check and closed a live in-flight build task within the hour.

A genuine return is symmetric; a forward request is not:

| | parent | new task | fires? |
|---|---|---|---|
| return | audit `developer → security` | `security → developer` | yes — both halves hold |
| forward request | build `research → developer` | audit request `developer → security` | no — `research != security` |

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
    include_dead_letters=False,  # include dead-letters/ subdirectory
    limit=20,  # max 200
)
# → list of task dicts, sorted by created descending
```

**An unrecognised `status` is an error, not an empty result (since v0.6.0).** It used to be filtered on silently, which is how a sweep for `status="pending"` — never a status here — returned `[]` for months, indistinguishable from "no work for you". An empty list is a legitimate answer to a well-formed question, so the only way to tell a typo apart from an empty queue is to refuse the typo. Whitespace and a trailing comma are still tolerated; an empty string still means no filter.

**Terminal** tasks past their `ttl_days` are excluded. The dispatcher is authoritative for TTL archiving, but `list_tasks` filters finished records out proactively so agents don't act on stale items.

Non-terminal tasks are **never** TTL-filtered (since v0.8.1, vikunja#395). Open work used to vanish from listings after `ttl_days` while still sitting on disk waiting for someone — a blind spot rather than a guard, and one that had already caused a queue sweep to find 17 stranded tasks where this tool reported 13. Nothing that is still someone's responsibility should be hidden by a clock: an agent handed a stale open task can judge it, whereas nobody can act on a task they cannot see.

**Parked tasks are exempt from the TTL filter.** Parking is a deliberate "pause this, I'll come back to it" — a parked task quietly expiring out of the listing would defeat the point of the status.

Every returned record carries **`queue_location`** — `"queue"`, `"archive"` or `"dead-letters"` — so a caller can tell a dead letter from live work without inspecting file paths (which it never sees).

#### Dead letters (since v0.10.0)

`include_dead_letters=True` also returns records the dispatcher gave up routing and moved to `dead-letters/`. It is **off by default and does not follow `include_archived`**: every agent's work sweep is a `list_tasks` call, and a dead letter is a task nothing can route, so folding them into the default listing would hand each agent a backlog it cannot act on. Visibility is the point, not re-delivery.

Two things make the flag actually work against a real queue, and both were found by running it against one:

- **Dead letters are exempt from the TTL filter.** A dead letter carries `failed` — terminal — and the seventeen on forge are between one and three months past their `ttl_days`. Without the exemption the flag returns an empty list, which reads as "there are none".
- **Dead letters sort first when included.** A dead letter is among the oldest records in the queue by construction; under a plain created-descending sort all seventeen land behind several hundred live tasks and `limit` discards every one. Measured: `include_dead_letters=True, limit=200` returned 200 rows and zero dead letters before this. Ordering *within* each group is unchanged.

### get_task

```python
get_task(task_id="a7f3d2c1-1234-5678-abcd-000000000000")
# → full task dict, or {"ok": false, "error": "not found"}
```

Searches the main queue first, then `archive/`, then `dead-letters/`. Requires a full UUID — no prefix matching.

A dead-lettered record comes back with its `failed_reason` block intact and `queue_location: "dead-letters"`. Unlike `list_tasks`, this needs no opt-in: asking for a task by id is not a work sweep, it is someone holding a specific id and wanting to know what became of it. Answering `not found` for a record sitting on disk was the bug — a dropped audit request looked identical to an id that never existed.

A dead letter cannot be transitioned in place. `update_task`, `set_task_status`, `park_task`/`unpark_task` and `amend_task` do not load `dead-letters/` at all — that absence *is* the gate — and refuse by name (`task is dead-lettered and cannot be mutated in place`) rather than answering `not found`. The one way out is `requeue_dead_letter`.

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

## HTTP Control and Read API

Non-MCP clients (the CloudCLI plugin, the Matrix bot, and an operator panel) can't import the Python core, so they read and write the queue through a thin HTTP API mounted as FastMCP custom routes on the **same port 8485**. Each endpoint delegates to the tool handlers above, inheriting transition validation, `fcntl` locking, atomic writes, and the TTL and dead-letter rules. There is one validated write path and, since v0.11.0, one read path, instead of every client parsing the queue YAML itself.

| Method | Path | Scope | Delegates to |
|--------|------|-------|--------------|
| `GET` | `/tasks` | `read` | `list_tasks`, with `count` and `truncated` (see below) |
| `GET` | `/tasks/{id}` | `read` | `get_task`: queue, `archive/`, then `dead-letters/` |
| `GET` | `/queue/summary` | `read` | Counts by status across the active queue, plus `dead_letters` |
| `POST` | `/tasks/{id}/approve` | `operator-write` | `set_task_status(approved)` |
| `POST` | `/tasks/{id}/cancel` | `operator-write` | `cancel_task` |
| `POST` | `/tasks/{id}/status` | `operator-write` | `set_task_status` (body: `status`, `note`, `allow_override`) |
| `POST` | `/tasks/{id}/park` | `operator-write` | `park_task` |
| `POST` | `/tasks/{id}/unpark` | `operator-write` | `unpark_task` (body: optional `status`) |
| `POST` | `/tasks/{id}/amend` | `operator-write` | `amend_task` (body: `amendment`, optional `reason`) |
| `POST` | `/tasks/{id}/update` | `operator-write` | `update_task` (body: `status`, `note`, `output`, optional `on_behalf_of`) |
| `POST` | `/tasks/{id}/requeue` | `operator-write` | `requeue_dead_letter` (body: optional `note`) |

Body fields: `note`, plus `status` / `allow_override` for the status route, `amendment` / `reason` for amend, `status` / `output` / `on_behalf_of` for update. Responses map the canonical result: `200` ok, `404` not found, `400` validation/transition error. Auth failures are `401` (no valid token) and `403` (valid token without the route's scope).

**`actor` is pinned to `operator` on every one of these routes and is not read from the body** (since v0.8.0). It was previously `body.get("actor", "operator")` — correct in practice, but it made the operator identity something a caller inherited by omission rather than something anyone chose. Pinning means a future non-operator client here cannot quietly acquire the identity that every ownership check exempts.

**Every write records its `channel`** (since v0.11.0): which client made it. `actor` says the operator acted; `channel` says through what.

```yaml
history:
  - timestamp: ...
    status: approved
    actor: operator
    channel: matrix-bot
    note: ""
```

MCP tool calls write no `channel`, and records written before v0.11.0 are not rewritten.

### Reading the queue — `GET /tasks`, `GET /tasks/{id}`

`GET /tasks` takes `target_agent`, `source_agent`, `status` (single or comma-separated), `task_type`, `include_archived`, `include_dead_letters` (`true`/`false`) and `limit` (default 200, max 1000). It returns:

```json
{"ok": true, "tasks": [...], "count": 612, "truncated": true}
```

`count` is how many records **matched**, not how many came back, and `truncated` is true whenever that is more than `len(tasks)`. The MCP tool returns a bare list and cannot say it cut anything off. That silence is how one dead-letter listing returned 200 rows and none of the records it was asked for. A client rendering this list should show `truncated`, not hide it.

An unknown status, an unknown query parameter (a misspelt filter would otherwise return the unfiltered queue), a non-boolean flag, or a `limit` outside 1–1000 is a `400`. A limit above the ceiling is refused rather than clamped, so a client learns the cap exists.

`GET /tasks/{id}` returns `{"ok": true, "task": {...}}`, `400` on a malformed id, `404` when no record has it. Timestamps are ISO 8601 strings, as on the MCP transport.

**Read performance (since v0.13.0).** Task files are parsed with libyaml's `CSafeLoader`
(the startup log names the loader in use). A lookup by id, for `get_task` and every
mutation, parses only the files named `*-<id[:8]>.yml`. If none of those holds the id, it
falls back to a full scan and logs a warning naming the misnamed file. So a record whose
filename does not match its id is still found, never reported as `404`. Every HTTP route runs
its handler on a worker thread, as FastMCP already does for MCP tools, so a slow scan no
longer holds up other callers. Concurrent writes to one task are still serialised by its
`fcntl` lock.

### The operator sweep — `POST /tasks/{id}/update`

The only path to a terminal transition on **another agent's** task. It exists because v0.8.0 closed the dishonest version: agents used to tidy up a stranded task by passing that agent's name as `actor`, which binding `actor` to a bearer token removes. Nothing else reaches it — `set_task_status` cannot make terminal transitions and the `update_task` *tool* now demands the resolved identity — so without this every stray would need the operator to intervene by hand.

Pass `on_behalf_of` naming the agent whose task it is. The handler verifies it against the task's actual `target_agent` (a mismatch is a `400`, because an operator closing a task they have misidentified should be told, not have the mistake recorded as deliberate) and writes **both** names into history:

```yaml
history:
  - timestamp: ...
    status: completed
    actor: operator
    on_behalf_of: developer
    note: "stranded; swept during queue cleanup"
```

A sweep should read as a sweep years later, not as the agent having quietly closed its own work. `on_behalf_of` is optional — omitting it is the operator acting in its own name — and is refused outright for any non-`operator` actor.

`GET /queue/summary` returns `{"ok": true, "counts": {...}, "active": N, "total": N, "dead_letters": N}`, where `active` is the non-terminal total (now including `routing-failed`, counted by name). Statuses outside the server's vocabulary entirely are bucketed under `"unknown"` rather than dropped, so records written by other direct-YAML writers stay visible in the count.

`dead_letters` is a **sibling** of `counts`, not a member of it (since v0.10.0). Every dead letter carries `failed`, so folding them into the status histogram would bury them among genuinely finished work — the same invisibility, one field along. `counts`, `active` and `total` all describe the active queue only.

### Recovering a dead letter — `POST /tasks/{id}/requeue`

Moves the record back to the queue root at `submitted`, drops `failed_reason`, and resets `retry_policy` to `{next_retry_at: null, retry_count: 0}`. `created` is **not** refreshed — when the work was first asked for is the record, and rewriting it to make a three-month-old dropped audit look new is the flavour of tidiness that made the backlog invisible. `alert_state` is left alone; the dispatcher owns it. The history entry records `action: requeue`, the actor, and `cleared_failed_reason`, so a second drop does not read as a first.

**Operator-only**, the same gate as `set_task_status`: if an agent could requeue its own dead letters, a routing bug that drops a task becomes an agent-driven retry loop and the dispatcher's retry ceiling bounds nothing.

**Terminal immutability is not weakened.** The handler looks the record up *only* under `dead-letters/`, so a `failed` task in the queue root or in `archive/` is unreachable here however its id is spelled. A dead letter's `failed` is the dispatcher's record of exhausting its retries, not an agent's judgement that the work is over.

Requeueing does not fix *why* a task was dropped. Sending one of the seventeen back through the routing that rejected it will dead-letter it again after three retries — that root cause is vikunja#63/#169.

### Auth: per-client scoped tokens (since v0.11.0)

Each client has its own token and its own scopes. The server stores only the token's sha256 digest:

```bash
TASK_QUEUE_CLIENT_CLOUDCLI=sha256:<64 lowercase hex>
TASK_QUEUE_CLIENT_SCOPES_CLOUDCLI=read,operator-write
```

- **Scopes:** `read` (the `GET` routes) and `operator-write` (the `POST` routes). The vocabulary is closed, and neither scope implies the other.
- **Channel name:** the suffix, lowercased with `_` → `-` (`MATRIX_BOT` → `matrix-bot`), the same rule agent names use.
- **Header:** send the plaintext token as `X-Task-Queue-Token: <token>`. **Never `Authorization`.** FastMCP's authentication middleware runs on every route, not only `/mcp`, and would offer a bearer to the agent-token verifier. These routes read their own header and ignore whatever FastMCP authenticated, so a valid agent bearer grants nothing here, and a client token grants nothing on `/mcp`.
- **Comparison:** the presented token is hashed and compared against every configured digest with `hmac.compare_digest`.
- **Logging:** refusals and writes are logged with the channel name, never the token or its digest.

Mint a client token and its digest:

```bash
python -c "import secrets, hashlib; t = secrets.token_urlsafe(32); print(t); print('sha256:' + hashlib.sha256(t.encode()).hexdigest())"
```

Give the first line to the client, in a file only it reads, and put the second in the server's environment.

**Why digests.** If the server held plaintext client tokens, anything that can read its env file could take any client's credential. With digests, reading the server's configuration yields nothing a request can use. A client whose plaintext lives under a separate OS user gets a real boundary from that. A client whose token file is readable by the same user as the agents on the host gets containment and attribution only: the token is revocable and no longer sits in every process's environment. It is not a boundary.

The server **refuses to start** on a malformed digest, a digest shared by two clients, a digest or scopes line without its partner, an empty or unknown scope, a channel named `operator` or `legacy-shared` or after an agent identity, or a client digest equal to an agent token's. Zero clients is valid: every custom route then refuses every request, and `/mcp` is unaffected.

**The shared secret is gone (v0.12.0).** `TASK_QUEUE_API_SECRET` and the `X-Task-Queue-Secret` header grant nothing. If the variable is still set, the server logs a startup warning telling you to delete it. A request that sends only the old header gets a 401 and a log line naming it. v0.11.0 accepted the secret as channel `legacy-shared` while clients migrated. That name stays reserved, because history entries written then still carry it.

## Deployment

### Docker (production)

Since v0.13.0 every release tag publishes an image to GHCR:
`ghcr.io/tadmstr/task-queue-mcp`, tagged `vX.Y.Z` and `X.Y.Z`, with `X.Y` and `latest`
moving only for non-prerelease tags. linux/amd64 only. Each image is smoke-tested before it
is pushed (libyaml present, the hardened flags below, token gate, UID 1000), and it carries
build provenance that the publishing run verifies (`gh attestation verify`) before it
finishes.

**Pin the tag and the digest.** The digest is in the GitHub Release notes for that tag and
in the publishing run's summary. A pull that resolves to a different digest is not the
image that was tested and verified.

```yaml
services:
  task-queue-mcp:
    image: ghcr.io/tadmstr/task-queue-mcp:v0.13.0@sha256:<digest from the Release notes>
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

`docker inspect` reports what is running: `org.opencontainers.image.version` and
`.revision` are set to the release tag and commit. A locally built image reports `dev` and
`unknown`. To run from source instead, replace `image:` with `build: <path to this repo>`
(see [Building](#building)).

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
| `TASK_QUEUE_CLIENT_<NAME>` | — | `sha256:<hex>` digest of one HTTP client's token, e.g. `TASK_QUEUE_CLIENT_CLOUDCLI`. Needs a matching `TASK_QUEUE_CLIENT_SCOPES_<NAME>`. See [Auth](#auth-per-client-scoped-tokens-since-v0110). |
| `TASK_QUEUE_CLIENT_SCOPES_<NAME>` | — | Comma-separated scopes for that client: `read`, `operator-write`, or both. |
| `TASK_QUEUE_API_SECRET` | — | **Removed in v0.12.0.** Ignored; if set, the server warns at startup. |
| `TASK_QUEUE_TOKEN_<AGENT>` | — | Bearer token for one calling agent, e.g. `TASK_QUEUE_TOKEN_DEVELOPER`. **At least one is required** — the HTTP transport refuses to start with none. The suffix becomes the agent identity, lowercased with `_` → `-` (`TASK_QUEUE_TOKEN_DOC_HEALTH` → `doc-health`). |

Each agent needs its **own** token — the token is what identifies the caller, so sharing one
between two agents makes attribution meaningless. The server refuses to start on a shared
token, an empty value, a token under 16 characters, or a token minted for the reserved
`operator` identity. Generate with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Callers present it as a standard bearer header:

```yaml
headers:
  Authorization: "Bearer ${TASK_QUEUE_TOKEN}"
```

## Building

```bash
docker build -t task-queue-mcp:latest .

# Optional: label the build the way the release workflow does.
docker build --build-arg VERSION=0.13.0 --build-arg REVISION="$(git rev-parse HEAD)" \
  -t task-queue-mcp:0.13.0 .
```

CI builds the image on every push and PR, and runs the same smoke test as the publish job.

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

The test suite covers every tool and the HTTP control API — validation edge cases, adversarial YAML strings, illegal transitions, the park/unpark round-trip, `amend_task` authorization (including the rejected target agent), operator-override auditing, out-of-vocabulary status repair, and the client-token scope gate (every custom route, enumerated from the app: no token → 401, wrong scope → 403, an agent bearer → 401). `tests/conftest.py` strips every task-queue credential variable from the environment before collection, so running the suite from a shell that holds real tokens neither uses them nor prints them. All writes use `yaml.dump` — never string interpolation — to prevent YAML injection.

## Security

Both surfaces on port 8485 require a credential:

- **MCP tool path** (`/mcp`) — a per-agent bearer token, verified by FastMCP's `StaticTokenVerifier`. Missing or unknown token → 401. The transport refuses to start with no tokens configured, so this cannot silently fail open.
- **HTTP control and read routes** (`/tasks...`, `/queue/summary`) — a per-client token in `X-Task-Queue-Token`, stored server-side as a sha256 digest, with a `read` or `operator-write` scope per route. See [HTTP Control and Read API](#http-control-and-read-api).

The container runs as UID 1000 with `cap_drop: ALL`, `no-new-privileges`, and a read-only rootfs (only `/task-queue` is writable).

### Trust model

**Until v0.7.0 the MCP tool path was unauthenticated** and the README argued that loopback was a sufficient trust boundary. It was not: the port is published *and* the container joins a shared Docker network, so every container on that network could reach the tool path too. Any of them could call `set_task_status`, `cancel_task`, `park_task`, `unpark_task`, or `amend_task` while asserting any `actor` — including `operator`, which the ownership checks explicitly exempt. That made `completed_by` and `history[].actor` claims rather than evidence. (vikunja#387)

v0.7.0 closes that path. Each agent holds a distinct token, so **the token both authenticates the caller and identifies it**. There is deliberately no separate identity header: once an agent holds a token it can set any header it likes on a direct request, so a header-derived identity would be a strictly weaker second channel competing with the token-derived one. One source of identity, not two.

**What this does and does not buy.** It contains a *mistaken or prompt-injected* agent acting through its own tool surface, and it makes the audit trail mean what it says. It is deliberately **not** a boundary against an agent that goes looking for credentials: where agents hold a shell tool and run as the same OS user that owns the secret files, any token on the host is readable by any of them. Closing that needs per-agent OS users or a credential broker, and is out of scope for this server.

The `operator` identity is reachable **only** from the HTTP control routes. A `TASK_QUEUE_TOKEN_OPERATOR` is rejected at startup, because `operator` is exempt from every ownership check and a token minting it on the agent-facing transport would hand its holder the whole queue.

### Identity binding (since v0.8.0)

`actor` is **derived from the bearer token**, not taken from the caller. Passing a name that does not match the authenticated identity is refused rather than silently corrected — the wrong name in a call is a bug worth surfacing. Omitting it is fine; it is filled in from the token.

This covers `source_agent` on `submit_task` too, which is an identity claim and not just a label: the submit-time auto-close decides whether to fire from `source_agent`/`target_agent`, so spoofing it would terminally close another agent's task without ever calling `update_task`.

| Tool | Who may call it |
|---|---|
| `submit_task`, `list_tasks`, `get_task` | any authenticated agent (`source_agent` is bound to the caller) |
| `update_task` | the task's `target_agent`, or the operator |
| `park_task`, `unpark_task` | the task's `target_agent`, or the operator |
| `amend_task` | the task's `source_agent`, or the operator |
| `set_task_status`, `cancel_task`, `requeue_dead_letter` | **operator only** — refused for any agent identity |

`set_task_status` is operator-only because its `allow_override` path moves a task between any two non-terminal statuses, which is how a task gets walked around a transition rule instead of satisfying it. `cancel_task` is a terminal, irreversible judgement about someone else's work; an agent abandoning its own task marks it `failed` with a reason via `update_task`.

## Task File Schema

Tasks are YAML files in `~/.claude/task-queue/`, named `YYYYMMDD-HHMMSS-<uuid-prefix>.yml`. All writes are atomic (write to `.tmp`, then `os.rename()`). Per-task file locks via `fcntl.flock` prevent races between concurrent MCP calls and the dispatcher.

For the full schema and lifecycle documentation, see the [homelab-agent component doc](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/agent/task-queue-mcp.md).

## Related

- [homelab-agent](https://github.com/TadMSTR/homelab-agent) — agent orchestration documentation
- [task-dispatcher](https://github.com/TadMSTR/homelab-agent/blob/main/docs/components/agent/task-dispatcher.md) — the dispatcher that routes and gates tasks
