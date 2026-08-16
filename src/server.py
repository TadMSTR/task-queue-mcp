import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.auth import (
    TOKEN_ENV_PREFIX,
    AuthConfigError,
    bind_actor,
    build_verifier,
    load_agent_tokens,
    require_operator_surface,
)
from src.tools.queue import (
    NON_TERMINAL_STATUSES,
    OPERATOR_ACTOR,
    VALID_STATUSES,
    _load_all_tasks,
    amend_task_handler,
    cancel_task_handler,
    get_task_handler,
    list_tasks_handler,
    park_task_handler,
    set_task_status_handler,
    submit_task_handler,
    unpark_task_handler,
    update_task_handler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

QUEUE_DIR = os.environ.get("TASK_QUEUE_DIR", "/task-queue")


@asynccontextmanager
async def lifespan(app):
    if not os.path.isdir(QUEUE_DIR):
        logger.error(
            "TASK_QUEUE_DIR=%s does not exist or is not a directory — exiting.",
            QUEUE_DIR,
        )
        sys.exit(1)
    logger.info("task-queue-mcp started. Queue dir: %s", QUEUE_DIR)
    yield
    logger.info("task-queue-mcp shutting down.")


# Per-agent bearer tokens for the MCP tool path (vikunja#387). A configuration error here
# is fatal by design: load_agent_tokens raises rather than dropping a bad entry, because
# every way this can be misconfigured — an empty var, a shared token, a token minted for
# `operator` — fails open or mis-attributes, and both are worse than not starting.
try:
    _agent_tokens = load_agent_tokens()
except AuthConfigError as exc:
    logger.error("Refusing to start: %s", exc)
    sys.exit(1)

mcp = FastMCP("task-queue", lifespan=lifespan, auth=build_verifier(_agent_tokens))


@mcp.tool()
def submit_task(
    source_agent: str,
    target_agent: str,
    task_type: str,
    summary: str,
    description: str,
    risk_level: str = "low",
    requires_approval: bool = False,
    priority: str = "normal",
    context_refs: list[str] | None = None,
    ttl_days: int = 30,
    workflow_mode: str = "semi-auto",
    originating_task_id: str | None = None,
) -> dict:
    """
    Submit a new task to the queue.
    task_type: build | deploy | fix | research | review | audit | notify | docs |
               ticket_audit | ticket_audit_complete
    risk_level: low | medium | high
    priority: normal | high | urgent
    workflow_mode: semi-auto | auto
    context_refs: list of absolute paths relevant to this task
    originating_task_id: UUID of the parent task. The dispatcher inherits its
      workflow_mode, and if that parent targets you and is approved or in-progress it is
      auto-closed as completed — submitting the return task IS closing the request.
    source_agent must be your own authenticated identity; you cannot file a task as
      another agent.
    Returns: {ok, task_id, filename} on success, plus auto_closed_task_id when a parent was
    closed; or {ok: false, error} on failure.
    """
    # source_agent is an identity claim, not just a label — the submit-time auto-close
    # decides the return shape from it, so spoofing it is a route to terminally closing
    # another agent's task without ever calling update_task.
    ok, source_agent = bind_actor(source_agent)
    if not ok:
        return {"ok": False, "error": source_agent}

    return submit_task_handler(
        source_agent=source_agent,
        target_agent=target_agent,
        task_type=task_type,
        summary=summary,
        description=description,
        risk_level=risk_level,
        requires_approval=requires_approval,
        priority=priority,
        context_refs=context_refs or [],
        ttl_days=ttl_days,
        workflow_mode=workflow_mode,
        originating_task_id=originating_task_id,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def list_tasks(
    target_agent: str | None = None,
    source_agent: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
) -> list:
    """
    List tasks from the queue with optional filters.
    status: single value or comma-separated (e.g. "submitted,approved"). Must be a real
      status — an unrecognised one is an error, not an empty result.
      Valid: submitted, approved, pending-approval, in-progress, parked, routing-failed,
      completed, failed, cancelled.
    Returns tasks sorted by created descending. Expired tasks (past ttl_days) are excluded
    only if they are terminal — open work stays listed however old it is, so nothing that
    is still someone's responsibility can quietly age out of view.
    """
    return list_tasks_handler(
        target_agent=target_agent,
        source_agent=source_agent,
        status=status,
        task_type=task_type,
        include_archived=include_archived,
        limit=limit,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def get_task(task_id: str) -> dict:
    """
    Get a task by UUID. Searches main queue then archive/.
    Returns full task dict or {ok: false, error}.
    """
    return get_task_handler(task_id=task_id, queue_dir=QUEUE_DIR)


@mcp.tool()
def update_task(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    output: str | None = None,
) -> dict:
    """
    Update task status and append a history entry.
    Valid transitions: approved→in-progress, in-progress→completed, any non-terminal→failed.
    output is written to result.output on completed or failed.
    actor is derived from your bearer token; passing another agent's name is refused.
    Returns {ok, task_id} or {ok: false, error}.
    """
    ok, actor = bind_actor(actor)
    if not ok:
        return {"ok": False, "error": actor}

    return update_task_handler(
        task_id=task_id,
        status=status,
        actor=actor,
        note=note,
        output=output,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def set_task_status(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    allow_override: bool = False,
) -> dict:
    """
    Operator status change (broader than update_task). Standard transitions:
    submitted/pending-approval→approved, any non-terminal→cancelled. Set
    allow_override=True (with a non-empty note) to advance a missed task between any
    two non-terminal statuses. Terminal tasks are immutable. Returns {ok, task_id}.

    OPERATOR ONLY — not reachable with an agent identity. The allow_override path can walk
    a task between any two non-terminal statuses, which is how a task gets moved around a
    transition rule it should have had to satisfy.
    """
    refusal = require_operator_surface("set_task_status")
    if refusal:
        return {"ok": False, "error": refusal}

    return set_task_status_handler(
        task_id=task_id,
        status=status,
        actor=actor,
        note=note,
        allow_override=allow_override,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def cancel_task(task_id: str, actor: str, note: str = "") -> dict:
    """
    Cancel a task — a graceful, audited terminal state for stale or unwanted tasks
    (use instead of mislabeling them `failed`). The record stays on disk. Returns
    {ok, task_id} or {ok: false, error}.

    OPERATOR ONLY — not reachable with an agent identity. Cancelling is a terminal,
    irreversible transition on someone else's work; deciding a task is no longer wanted is
    an operator judgement. An agent abandoning its own task should mark it `failed` with a
    reason via update_task.
    """
    refusal = require_operator_surface("cancel_task")
    if refusal:
        return {"ok": False, "error": refusal}

    return cancel_task_handler(task_id=task_id, actor=actor, note=note, queue_dir=QUEUE_DIR)


@mcp.tool()
def park_task(task_id: str, actor: str, note: str = "") -> dict:
    """
    Park a task — pause it without losing sight of it. The task stays in the queue and
    keeps appearing in list_tasks, but nothing will pick it up until it is unparked, and
    it is exempt from TTL expiry. Use for "not now, but don't lose this". Reversible via
    unpark_task, which returns it to the status it was parked from.
    You may park a task addressed to you; the operator may park any task.
    Returns {ok, task_id} or {ok: false, error}.
    """
    ok, actor = bind_actor(actor)
    if not ok:
        return {"ok": False, "error": actor}

    return park_task_handler(
        task_id=task_id,
        actor=actor,
        note=note,
        queue_dir=QUEUE_DIR,
        enforce_ownership=True,
    )


@mcp.tool()
def unpark_task(task_id: str, actor: str, note: str = "", status: str | None = None) -> dict:
    """
    Unpark a task, returning it to the status it was parked from. Pass status to send it
    somewhere else instead. Reverses park_task.
    You may unpark a task addressed to you; the operator may unpark any task.
    Returns {ok, task_id} or {ok: false, error}.
    """
    ok, actor = bind_actor(actor)
    if not ok:
        return {"ok": False, "error": actor}

    return unpark_task_handler(
        task_id=task_id,
        actor=actor,
        note=note,
        status=status,
        queue_dir=QUEUE_DIR,
        enforce_ownership=True,
    )


@mcp.tool()
def amend_task(task_id: str, amendment: str, actor: str, reason: str = "") -> dict:
    """
    Append a correction to a queued task without rewriting it. The original description is
    never modified — amendments accumulate under payload.amendments and readers render them
    after it. Use when something changes between queuing and starting: a preflight answers
    an open question, a dependency lands, scope narrows.

    Only the task's source_agent or "operator" may amend; the target agent may not.
    Permitted on non-terminal tasks including in-progress ones — check
    agent_may_have_started in the response, since the agent may already have read the
    original. More than one or two amendments is a signal to cancel and re-queue instead.

    Returns {ok, task_id, amendment_count, agent_may_have_started} or {ok: false, error}.
    """
    ok, actor = bind_actor(actor)
    if not ok:
        return {"ok": False, "error": actor}

    return amend_task_handler(
        task_id=task_id, amendment=amendment, actor=actor, reason=reason, queue_dir=QUEUE_DIR
    )


# ---------------------------------------------------------------------------
# HTTP control API — the single validated mutation path for non-MCP clients
# (the CloudCLI plugin and the Matrix bot). Mounted as custom routes on the
# existing FastMCP HTTP app, so it shares this container and port 8485.
#
# These routes are NOT behind the MCP bearer auth added in v0.7.0 — custom_route
# handlers bypass the transport's auth provider — so TASK_QUEUE_API_SECRET remains
# their only gate. Until v0.7.0 that sentence read as though an MCP auth middleware
# already existed; it did not, and the tool path was open (vikunja#387). It does now,
# and these routes are deliberately outside it: they are the operator surface, and the
# operator identity is reachable from here and nowhere else.
#
# Note this gate contains mistakes rather than intent: wherever an agent holds a shell
# tool and runs as the OS user that owns the secret file, it can read the secret. Closing
# that needs per-agent OS users or a credential broker, not a change here.
#
# Every route delegates to the same handlers as the MCP tools, inheriting transition
# validation + fcntl locking + atomic writes. Reads stay direct in the clients.
# ---------------------------------------------------------------------------

SECRET_HEADER = "X-Task-Queue-Secret"

# These routes ARE the operator surface, so the actor is pinned rather than defaulted.
# It was previously `body.get("actor", "operator")` on all six mutation routes: correct in
# practice, but it made the operator identity something a caller inherited by omission
# rather than something anyone chose. Pinning it means a future non-operator client on
# these routes cannot quietly acquire the identity that every ownership check exempts —
# it would have to be given its own path, deliberately.
#
# This is narrower than it may look. The shared secret is what gates these routes, and any
# caller that holds it can already assert this identity; pinning removes an accident, not
# an attack. See the note on the control-API block above.
#
# OPERATOR_ACTOR is imported from src.tools.queue — it was defined here as a second copy of
# the same literal until the 2026-08-16 audit caught it (LOW).


def _authorized(request: Request) -> bool:
    """Constant-time shared-secret check. Fails closed when no secret is configured."""
    secret = os.environ.get("TASK_QUEUE_API_SECRET", "")
    if not secret:
        logger.warning("TASK_QUEUE_API_SECRET not configured — rejecting control-API request")
        return False
    provided = request.headers.get(SECRET_HEADER, "")
    # Compare as bytes — hmac.compare_digest raises TypeError on str operands with
    # non-ASCII chars, so a malformed header must not escape as a 500. (audit L-02)
    return hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))


async def _json_body(request: Request) -> dict:
    """Parse a JSON request body, tolerating an empty body. Returns {} on empty/invalid."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _status_for(result: dict) -> int:
    if result.get("ok"):
        return 200
    if result.get("error") == "not found":
        return 404
    return 400


def _control_response(result: dict) -> JSONResponse:
    return JSONResponse(result, status_code=_status_for(result))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


@mcp.custom_route("/tasks/{task_id}/approve", methods=["POST"])
async def http_approve(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = set_task_status_handler(
        task_id=request.path_params["task_id"],
        status="approved",
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/cancel", methods=["POST"])
async def http_cancel(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = cancel_task_handler(
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/status", methods=["POST"])
async def http_set_status(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = set_task_status_handler(
        task_id=request.path_params["task_id"],
        status=body.get("status", ""),
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        allow_override=bool(body.get("allow_override", False)),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/park", methods=["POST"])
async def http_park(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = park_task_handler(
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/unpark", methods=["POST"])
async def http_unpark(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = unpark_task_handler(
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        status=body.get("status"),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/amend", methods=["POST"])
async def http_amend(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = amend_task_handler(
        task_id=request.path_params["task_id"],
        amendment=body.get("amendment", ""),
        actor=OPERATOR_ACTOR,
        reason=body.get("reason", ""),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/tasks/{task_id}/update", methods=["POST"])
async def http_update(request: Request) -> JSONResponse:
    """
    The operator's path to a terminal transition, including on another agent's behalf.

    This exists because the previous release closed the dishonest version of it. Sweeping
    another agent's stranded task used to be possible from any agent session by passing
    that agent's name as `actor` — 17 tasks were tidied up that way, honestly annotated,
    and only possible because `actor` was a free string. Binding `actor` to a bearer token
    removes that, and nothing else reaches it: `set_task_status` cannot make terminal
    transitions and the `update_task` tool now demands the resolved identity.

    Leaving it there would mean every future stray needs the operator to intervene by hand,
    so the capability is kept and made explicit instead. Pass `on_behalf_of` naming the
    agent whose task it is; the handler verifies that against the task's target_agent and
    records both names in history. A sweep should read as a sweep years later, not as the
    agent having quietly closed its own work.
    """
    if not _authorized(request):
        return _unauthorized()
    body = await _json_body(request)
    result = update_task_handler(
        task_id=request.path_params["task_id"],
        status=body.get("status", ""),
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        output=body.get("output"),
        on_behalf_of=body.get("on_behalf_of"),
        queue_dir=QUEUE_DIR,
    )
    return _control_response(result)


@mcp.custom_route("/queue/summary", methods=["GET"])
async def http_queue_summary(request: Request) -> JSONResponse:
    """
    Counts by status across the active queue. Statuses outside VALID_STATUSES are bucketed
    under "unknown" rather than dropped, so records written by other direct-YAML writers
    (the dispatcher's `routing-failed`, or historic typos) stay visible.
    """
    if not _authorized(request):
        return _unauthorized()

    counts: dict[str, int] = {}
    unknown = 0
    for task in _load_all_tasks(QUEUE_DIR):
        status = task.get("status")
        if status in VALID_STATUSES:
            counts[status] = counts.get(status, 0) + 1
        else:
            unknown += 1
    if unknown:
        counts["unknown"] = unknown

    active = sum(n for s, n in counts.items() if s in NON_TERMINAL_STATUSES)
    return JSONResponse(
        {"ok": True, "counts": counts, "active": active, "total": sum(counts.values())}
    )


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8485"))

    # Fail closed. This is the only transport this server is ever started with, and it is
    # reachable both from the published port and from the container network it joins.
    # Starting it without tokens is precisely the vikunja#387 state, so it must not happen
    # quietly because a secrets file failed to mount.
    if not _agent_tokens:
        logger.error(
            "Refusing to start the HTTP transport with no agent tokens configured. "
            "Set at least one %s<AGENT> — an unauthenticated :%d is vikunja#387.",
            TOKEN_ENV_PREFIX,
            port,
        )
        sys.exit(1)

    logger.info(
        "MCP tool path authenticated for %d agent(s): %s",
        len(_agent_tokens),
        ", ".join(sorted(_agent_tokens.values())),
    )
    mcp.run(transport="streamable-http", host=host, port=port)
