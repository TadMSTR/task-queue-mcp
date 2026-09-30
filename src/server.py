import functools
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime

import anyio.to_thread
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from src.auth import (
    RETIRED_SECRET_ENV,
    SCOPE_OPERATOR_WRITE,
    SCOPE_READ,
    TOKEN_ENV_PREFIX,
    AuthConfigError,
    Client,
    authorize_client,
    bind_actor,
    build_verifier,
    load_agent_tokens,
    load_client_tokens,
    require_operator_surface,
    retired_secret_set,
)
from src.tools.queue import (
    LIST_PAGE_MAX,
    NON_TERMINAL_STATUSES,
    OPERATOR_ACTOR,
    VALID_STATUSES,
    YAML_LOADER_NAME,
    _load_all_tasks,
    amend_task_handler,
    cancel_task_handler,
    count_dead_letters,
    get_task_handler,
    list_tasks_handler,
    list_tasks_page_handler,
    park_task_handler,
    requeue_dead_letter_handler,
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
    # Named at startup so a slow deployment can be diagnosed from its log: SafeLoader here
    # means PyYAML lost libyaml, and every read is ~10x slower (vikunja#1003).
    logger.info("YAML loader: %s", YAML_LOADER_NAME)
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
      `notify` is SELF-TERMINAL: the task is written straight to `completed`, no agent is
      ever launched for it and nobody has to close it. Use it for "here is a result,
      nothing to do" — a verdict, an outcome, an FYI. Do NOT use it to ask for work: a
      notify task never reaches anyone's work list, so the request would vanish silently.
      `requires_approval` is forced False for this type.
    risk_level: low | medium | high
    priority: normal | high | urgent
    workflow_mode: semi-auto | auto | manual-then-auto
      `manual-then-auto` gates only THIS task — it waits for an operator Start exactly like
      `semi-auto` — while every task the resulting session spawns inherits `auto`. Use it to
      start a headless chain once and let the rest of it run itself.
    context_refs: list of absolute paths relevant to this task
    originating_task_id: UUID of the parent task. The dispatcher inherits its
      workflow_mode, and if that parent targets you and is approved or in-progress it is
      auto-closed as completed — submitting the return task IS closing the request. This
      still holds for `notify`: a notification closes the request it answers.
    source_agent must be your own authenticated identity; you cannot file a task as
      another agent.
    Returns: {ok, task_id, filename} on success, plus auto_closed_task_id when a parent was
    closed and status/self_terminal for a self-terminal type; or {ok: false, error} on
    failure.
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
    include_dead_letters: bool = False,
    limit: int = 20,
) -> list:
    """
    List tasks from the queue with optional filters.
    status: single value or comma-separated (e.g. "submitted,approved"). Must be a real
      status — an unrecognised one is an error, not an empty result.
      Valid: submitted, approved, pending-approval, in-progress, parked, routing-failed,
      completed, failed, cancelled.
    include_dead_letters: also return records the dispatcher gave up routing and moved to
      dead-letters/. OFF by default — a dead letter is not actionable work and must not
      appear in a work sweep. Turn it on to audit the failure path. Dead letters are
      exempt from the TTL filter, so old ones still appear.
    Every record carries `queue_location` — "queue", "archive" or "dead-letters" — so a
      dead letter is distinguishable from live work without inspecting file paths.
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
        include_dead_letters=include_dead_letters,
        limit=limit,
        queue_dir=QUEUE_DIR,
    )


@mcp.tool()
def get_task(task_id: str) -> dict:
    """
    Get a task by UUID. Searches main queue, then archive/, then dead-letters/.
    A dead-lettered record comes back with its `failed_reason` block intact and
    `queue_location: "dead-letters"` — it is a record of a dropped task, not live work,
    and cannot be transitioned until an operator requeues it.
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
def requeue_dead_letter(task_id: str, actor: str, note: str = "") -> dict:
    """
    Return a dead-lettered task to the active queue at `submitted`, clearing its
    failed_reason and resetting its retry count. The requeue is recorded in history.

    OPERATOR ONLY — not reachable with an agent identity, the same gate as
    set_task_status. Resurrecting a dropped task is an operator judgement: if an agent
    could requeue its own dead letters, a routing bug that drops a task becomes an
    agent-driven retry loop with nothing bounding it. The retry ceiling the dispatcher
    enforces would be a ceiling on nothing.

    Only records under dead-letters/ are reachable here — a `failed` task in the live
    queue or in archive/ is not, whatever id is passed. Requeueing does not fix why the
    task was dropped; if the cause is still live it will dead-letter again.
    """
    refusal = require_operator_surface("requeue_dead_letter")
    if refusal:
        return {"ok": False, "error": refusal}

    return requeue_dead_letter_handler(task_id=task_id, actor=actor, note=note, queue_dir=QUEUE_DIR)


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
# HTTP control and read API — the operator surface for non-MCP clients (the CloudCLI
# plugin, the Matrix bot, and from operator-panel part 4 the panel). Mounted as custom
# routes on the existing FastMCP HTTP app, so it shares this container and port 8485.
#
# Each client authenticates with its own token in X-Task-Queue-Token and holds explicit
# scopes: `read` for the GET routes, `operator-write` for the POST routes. Neither implies
# the other. See the client-token section of src/auth.py for why the server stores only
# digests and why the header is not Authorization.
#
# FastMCP's AuthenticationMiddleware runs on these routes too, not only on /mcp, so a valid
# agent bearer DOES authenticate the request at the Starlette layer. Only /mcp enforces it.
# These routes ignore that result: authorize_client reads the client header and nothing
# else, so an agent identity grants nothing here. (Until v0.11.0 this comment said
# custom routes "bypass the transport's auth provider". True for enforcement only.)
#
# Every route delegates to the same handlers as the MCP tools, inheriting transition
# validation, fcntl locking and atomic writes. Reads used to stay direct in the clients,
# which made each client another reader of the queue YAML with its own copy of the TTL,
# dead-letter and status rules. v0.11.0 reverses that: the read routes wrap the handlers
# that already carry those rules.
#
# This contains mistakes rather than intent: the cloudcli and matrix-bot tokens sit in
# files `ted` can read, and every agent runs as `ted`. What changed is that no credential
# for this API is in any process's environment. Only a client whose plaintext lives under
# another UID (the panel, part 4) gets a real boundary out of this.
# ---------------------------------------------------------------------------

# These routes ARE the operator surface, so the actor is pinned rather than defaulted.
# It was previously `body.get("actor", "operator")` on all six mutation routes: correct in
# practice, but it made the operator identity something a caller inherited by omission
# rather than something anyone chose. Pinning it means a future non-operator client on
# these routes cannot quietly acquire the identity that every ownership check exempts —
# it would have to be given its own path, deliberately. Which client it was is recorded
# separately, as `channel` on the history entry.
#
# OPERATOR_ACTOR is imported from src.tools.queue — it was defined here as a second copy of
# the same literal until the 2026-08-16 audit caught it (LOW).

# Client tokens, digest -> Client. Loaded at import like the agent tokens, and fatal on a
# bad configuration for the same reason: every way it can be wrong fails open or
# mis-attributes. Zero clients is allowed and refuses every custom-route request.
try:
    _client_tokens = load_client_tokens(agent_tokens=_agent_tokens)
except AuthConfigError as exc:
    logger.error("Refusing to start: %s", exc)
    sys.exit(1)

# (method, path) -> required scope, filled in by _control_route. Tests enumerate the app's
# routes against this so a route added without a scope is caught.
ROUTE_SCOPES: dict[tuple[str, str], str] = {}


def _json_default(value):
    # The YAML loader produces datetimes and dates for timestamp fields. The MCP transport
    # serialises them as ISO 8601 through pydantic, and the HTTP routes must match it.
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _json(payload: dict, status_code: int = 200) -> Response:
    return Response(
        json.dumps(payload, default=_json_default),
        status_code=status_code,
        media_type="application/json",
    )


def _unauthorized() -> Response:
    return _json({"ok": False, "error": "unauthorized"}, status_code=401)


def _control_route(path: str, method: str, scope: str):
    """
    Register a custom route behind the client-token gate with one required scope.

    The wrapped handler receives the authenticated Client. A request with no valid token is
    401. A valid token without the route's scope is 403, which tells a client its
    credential works but was never granted this.
    """

    def register(handler):
        async def endpoint(request: Request) -> Response:
            client = authorize_client(request.headers, _client_tokens)
            if client is None:
                logger.warning("control-api: %s %s refused: no valid client token", method, path)
                return _unauthorized()
            if scope not in client.scopes:
                logger.warning(
                    "control-api: %s %s refused: channel %s lacks scope %s",
                    method,
                    path,
                    client.channel,
                    scope,
                )
                return _json({"ok": False, "error": f"scope {scope} required"}, status_code=403)
            log = logger.info if scope == SCOPE_OPERATOR_WRITE else logger.debug
            log("control-api: %s %s channel=%s", method, request.url.path, client.channel)
            return await handler(request, client)

        endpoint.__name__ = handler.__name__
        endpoint.__doc__ = handler.__doc__
        ROUTE_SCOPES[(method, path)] = scope
        mcp.custom_route(path, methods=[method])(endpoint)
        return endpoint

    return register


async def _offload(fn, /, **kwargs):
    """
    Run a blocking queue handler on a worker thread and await its result.

    Every handler parses YAML from disk. Called directly from these `async def` routes, one
    slow scan held the event loop and every other HTTP caller waited behind it. FastMCP
    already runs each sync `@mcp.tool` this way (anyio.to_thread.run_sync), so the MCP path
    and the HTTP path now share one concurrency model. Mutations are safe off the loop for
    the reason MCP tool calls already were: _task_lock is fcntl.flock on a freshly opened
    file, and flock locks belong to the open file description, so two threads in this
    process exclude each other (test_control_api's concurrency tests prove it).

    Only the handler call moves. Auth and body parsing stay on the loop.
    """
    return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))


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


def _control_response(result: dict) -> Response:
    return _json(result, status_code=_status_for(result))


# ── operator writes (scope: operator-write) ─────────────────────────────


@_control_route("/tasks/{task_id}/approve", "POST", SCOPE_OPERATOR_WRITE)
async def http_approve(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        set_task_status_handler,
        task_id=request.path_params["task_id"],
        status="approved",
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/cancel", "POST", SCOPE_OPERATOR_WRITE)
async def http_cancel(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        cancel_task_handler,
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/status", "POST", SCOPE_OPERATOR_WRITE)
async def http_set_status(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        set_task_status_handler,
        task_id=request.path_params["task_id"],
        status=body.get("status", ""),
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        allow_override=bool(body.get("allow_override", False)),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/park", "POST", SCOPE_OPERATOR_WRITE)
async def http_park(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        park_task_handler,
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/unpark", "POST", SCOPE_OPERATOR_WRITE)
async def http_unpark(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        unpark_task_handler,
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        status=body.get("status"),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/amend", "POST", SCOPE_OPERATOR_WRITE)
async def http_amend(request: Request, client: Client) -> Response:
    body = await _json_body(request)
    result = await _offload(
        amend_task_handler,
        task_id=request.path_params["task_id"],
        amendment=body.get("amendment", ""),
        actor=OPERATOR_ACTOR,
        reason=body.get("reason", ""),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/update", "POST", SCOPE_OPERATOR_WRITE)
async def http_update(request: Request, client: Client) -> Response:
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
    records both names in history, alongside the channel. A sweep should read as a sweep
    years later, not as the agent having quietly closed its own work.
    """
    body = await _json_body(request)
    result = await _offload(
        update_task_handler,
        task_id=request.path_params["task_id"],
        status=body.get("status", ""),
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        output=body.get("output"),
        on_behalf_of=body.get("on_behalf_of"),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


@_control_route("/tasks/{task_id}/requeue", "POST", SCOPE_OPERATOR_WRITE)
async def http_requeue(request: Request, client: Client) -> Response:
    """
    The operator's path to recovering a dead-lettered task. The operator-write scope is
    what makes it operator-only in practice: the MCP tool of the same name refuses any
    resolved agent identity, and these custom routes are where OPERATOR_ACTOR is
    assertable and nowhere else.
    """
    body = await _json_body(request)
    result = await _offload(
        requeue_dead_letter_handler,
        task_id=request.path_params["task_id"],
        actor=OPERATOR_ACTOR,
        note=body.get("note", ""),
        queue_dir=QUEUE_DIR,
        channel=client.channel,
    )
    return _control_response(result)


# ── reads (scope: read) ─────────────────────────────────────────────────

# Every query parameter GET /tasks understands. Anything else is a 400: a misspelt filter
# that was silently ignored would return the unfiltered queue, which reads as an answer.
LIST_QUERY_PARAMS = frozenset(
    {
        "target_agent",
        "source_agent",
        "status",
        "task_type",
        "include_archived",
        "include_dead_letters",
        "limit",
    }
)
LIST_DEFAULT_LIMIT = 200
_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALSE_VALUES = frozenset({"false", "0", "no"})


def _query_bool(params, name: str) -> bool:
    raw = params.get(name)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _query_limit(params) -> int:
    raw = params.get("limit")
    if raw is None:
        return LIST_DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        raise ValueError(f"limit must be an integer, got {raw!r}") from None
    # Out of range is refused, not clamped. A client asking for 5000 and silently getting
    # 1000 would still be told by `truncated`, but it would never learn the cap exists.
    if not 1 <= limit <= LIST_PAGE_MAX:
        raise ValueError(f"limit must be between 1 and {LIST_PAGE_MAX}, got {limit}")
    return limit


@_control_route("/tasks", "GET", SCOPE_READ)
async def http_list_tasks(request: Request, client: Client) -> Response:
    """
    list_tasks over HTTP. Returns {ok, tasks, count, truncated}.

    `count` is how many records matched, and `truncated` is true when that is more than
    were returned. Filters, TTL exemptions, dead-letter handling and ordering are
    list_tasks_handler's, unchanged; only the ceiling differs (LIST_PAGE_MAX, not 200).
    """
    params = request.query_params
    unknown = sorted(set(params.keys()) - LIST_QUERY_PARAMS)
    if unknown:
        return _json(
            {
                "ok": False,
                "error": f"unknown query parameter(s) {unknown}. "
                f"Valid: {sorted(LIST_QUERY_PARAMS)}",
            },
            status_code=400,
        )
    try:
        page = await _offload(
            list_tasks_page_handler,
            target_agent=params.get("target_agent") or None,
            source_agent=params.get("source_agent") or None,
            status=params.get("status") or None,
            task_type=params.get("task_type") or None,
            include_archived=_query_bool(params, "include_archived"),
            include_dead_letters=_query_bool(params, "include_dead_letters"),
            limit=_query_limit(params),
            queue_dir=QUEUE_DIR,
        )
    except ValueError as exc:
        # An invalid status raises from the handler, as it does for the MCP tool. Over HTTP
        # that is the caller's mistake, a 400, not a 500.
        return _json({"ok": False, "error": str(exc)}, status_code=400)
    return _json({"ok": True, **page})


@_control_route("/tasks/{task_id}", "GET", SCOPE_READ)
async def http_get_task(request: Request, client: Client) -> Response:
    """
    get_task over HTTP: searches the queue, archive/ and dead-letters/. Returns
    {ok, task}; 400 on a malformed id, 404 when no record has it.
    """
    result = await _offload(
        get_task_handler, task_id=request.path_params["task_id"], queue_dir=QUEUE_DIR
    )
    if result.get("ok") is False:
        return _control_response(result)
    return _json({"ok": True, "task": result})


@_control_route("/queue/summary", "GET", SCOPE_READ)
async def http_queue_summary(request: Request, client: Client) -> Response:
    """
    Counts by status across the active queue. Statuses outside VALID_STATUSES are bucketed
    under "unknown" rather than dropped, so records written by other direct-YAML writers
    (the dispatcher's `routing-failed`, or historic typos) stay visible.

    `dead_letters` is a sibling of `counts`, not a member of it. Every dead letter carries
    the status `failed`, so folding them into the status histogram would bury them among
    genuinely finished work — which is how seventeen of them went uncounted by every
    interface for three months. `counts`, `active` and `total` all describe the ACTIVE
    queue only; `dead_letters` is the number of records the dispatcher gave up on.
    """
    return _json(await _offload(_queue_summary, queue_dir=QUEUE_DIR))


def _queue_summary(queue_dir: str) -> dict:
    """http_queue_summary's body: blocking disk reads, so it runs on a worker thread."""
    counts: dict[str, int] = {}
    unknown = 0
    for task in _load_all_tasks(queue_dir):
        status = task.get("status")
        if status in VALID_STATUSES:
            counts[status] = counts.get(status, 0) + 1
        else:
            unknown += 1
    if unknown:
        counts["unknown"] = unknown

    active = sum(n for s, n in counts.items() if s in NON_TERMINAL_STATUSES)
    return {
        "ok": True,
        "counts": counts,
        "active": active,
        "total": sum(counts.values()),
        "dead_letters": count_dead_letters(queue_dir),
    }


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
    # Channel names and scopes only. Never a digest: it is not a secret, but it is the
    # thing a leaked token is matched against, and a log has no reason to carry it.
    logger.info(
        "control API: %d client(s): %s",
        len(_client_tokens),
        ", ".join(
            f"{c.channel}[{','.join(sorted(c.scopes))}]"
            for c in sorted(_client_tokens.values(), key=lambda c: c.channel)
        )
        or "none — every control and read route will refuse",
    )
    if retired_secret_set():
        # Reported, never honoured. Loud because a leftover copy of this variable is the
        # ambient credential v0.11.0-v0.12.0 existed to remove (vikunja#396).
        logger.warning(
            "control API: %s is set but IGNORED: the shared secret was removed in v0.12.0 "
            "and grants nothing. Delete it from this service's environment and anything "
            "that shares its env file.",
            RETIRED_SECRET_ENV,
        )
    mcp.run(transport="streamable-http", host=host, port=port)
