import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.tools.queue import (
    NON_TERMINAL_STATUSES,
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


mcp = FastMCP("task-queue", lifespan=lifespan)


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
    risk_level: low | medium | high
    priority: normal | high | urgent
    workflow_mode: semi-auto | auto
    context_refs: list of absolute paths relevant to this task
    originating_task_id: UUID of the parent task; dispatcher inherits its workflow_mode
    Returns: {ok, task_id, filename} on success or {ok: false, error} on failure.
    """
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
    status: single value or comma-separated (e.g. "submitted,approved")
    Returns tasks sorted by created descending. Expired tasks (past ttl_days) are excluded.
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
    Returns {ok, task_id} or {ok: false, error}.
    """
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
    """
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
    """
    return cancel_task_handler(task_id=task_id, actor=actor, note=note, queue_dir=QUEUE_DIR)


@mcp.tool()
def park_task(task_id: str, actor: str, note: str = "") -> dict:
    """
    Park a task — pause it without losing sight of it. The task stays in the queue and
    keeps appearing in list_tasks, but nothing will pick it up until it is unparked, and
    it is exempt from TTL expiry. Use for "not now, but don't lose this". Reversible via
    unpark_task, which returns it to the status it was parked from.
    Returns {ok, task_id} or {ok: false, error}.
    """
    return park_task_handler(task_id=task_id, actor=actor, note=note, queue_dir=QUEUE_DIR)


@mcp.tool()
def unpark_task(task_id: str, actor: str, note: str = "", status: str | None = None) -> dict:
    """
    Unpark a task, returning it to the status it was parked from. Pass status to send it
    somewhere else instead. Reverses park_task.
    Returns {ok, task_id} or {ok: false, error}.
    """
    return unpark_task_handler(
        task_id=task_id, actor=actor, note=note, status=status, queue_dir=QUEUE_DIR
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
    return amend_task_handler(
        task_id=task_id, amendment=amendment, actor=actor, reason=reason, queue_dir=QUEUE_DIR
    )


# ---------------------------------------------------------------------------
# HTTP control API — the single validated mutation path for non-MCP clients
# (the CloudCLI plugin and the Matrix bot). Mounted as custom routes on the
# existing FastMCP HTTP app, so it shares this container and port 8485.
#
# These routes are NOT behind the MCP auth middleware, so the shared secret is
# the only gate (defense-in-depth on top of the loopback-published port). Every
# route delegates to the same handlers as the MCP tools, inheriting transition
# validation + fcntl locking + atomic writes. Reads stay direct in the clients.
# ---------------------------------------------------------------------------

SECRET_HEADER = "X-Task-Queue-Secret"


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
        actor=body.get("actor", "operator"),
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
        actor=body.get("actor", "operator"),
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
        actor=body.get("actor", "operator"),
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
        actor=body.get("actor", "operator"),
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
        actor=body.get("actor", "operator"),
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
        actor=body.get("actor", "operator"),
        reason=body.get("reason", ""),
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
    mcp.run(transport="streamable-http", host=host, port=port)
