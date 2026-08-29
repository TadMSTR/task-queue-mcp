import fcntl
import glob
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import yaml

logger = logging.getLogger(__name__)

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_STATUSES = {
    "submitted",
    "approved",
    "pending-approval",
    "in-progress",
    "parked",
    "routing-failed",
    "completed",
    "failed",
    "cancelled",
}
VALID_PRIORITIES = {"normal", "high", "urgent"}
# `docs` is the writer's work-list type, introduced when doc-update-queue.jsonl was retired
# (task-queue-lifecycle-and-doc-queue-2026-08 Phase 5). `ticket_audit` and
# `ticket_audit_complete` were already documented in research's and security's CLAUDE.md and
# in the vikunja ticket-audit workflow — every such submit_task call failed validation here
# until they were added.
VALID_TASK_TYPES = {
    "build",
    "deploy",
    "fix",
    "research",
    "review",
    "audit",
    "notify",
    "docs",
    "ticket_audit",
    "ticket_audit_complete",
}
# `manual-then-auto` gates only its OWN leg: the dispatcher queues it for operator pickup
# exactly like `semi-auto`, but every task spawned by the resulting session inherits `auto`.
# It answers a different question from the other two — those describe how this task starts,
# this one describes the shape of the chain hanging off it. Added by
# task-queue-headless-chain-2026-08 (vikunja#533) because an operator's `semi-auto` start
# pinned every downstream handoff to `semi-auto` too, which is how four security→steward
# return tasks sat unactioned from 2026-08-18.
#
# The downgrade itself lives in the dispatcher (`child_workflow_mode()`), not here — this
# module never spawns anything. What this set does is make the value expressible at all.
VALID_WORKFLOW_MODES = {"semi-auto", "auto", "manual-then-auto"}

# Task types that are self-terminal: recorded and closed by `submit_task`, never launched,
# never queued for anyone. A `notify` task carries a result somebody may want to read, not
# work anybody has to do. (vikunja#507)
#
# SECURITY: this bypasses the approval path by design, so the bound that matters is that a
# `notify` task must never be able to carry an instruction an agent would act on. Two things
# hold that line: `requires_approval` is forced False (there is nothing left to approve once
# the task is already terminal, so honouring it would only produce an unreachable state),
# and the task is never `approved`, so it cannot appear in the `list_tasks(status="approved")`
# sweep every agent uses as its work list. Note it IS still visible to an unfiltered
# `list_tasks` until its TTL expires — terminal records age out on the clock, they are not
# excluded by status — which is the intent: readable, not assignable.
#
# Converting an ACTIONABLE return task to `notify` would therefore silently drop real work.
# See CHANGELOG for the one call site that was converted and the three that deliberately
# were not.
SELF_TERMINAL_TASK_TYPES = {"notify"}

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
NON_TERMINAL_STATUSES = VALID_STATUSES - TERMINAL_STATUSES

# Valid source statuses for each target transition in update_task (agent-facing, strict).
# NB: `cancelled` is operator-only and is NOT reachable here — agents cannot cancel.
#
# SECURITY[fixed]: `failed` used to be defined in terms of NON_TERMINAL_STATUSES, so adding
# `parked` to the vocabulary silently admitted it here — an agent that could still see a
# parked task via list_tasks could mark it `failed`, terminally ending a task the operator
# deliberately paused. Closed 2026-08-11 two ways: this set is now a literal (a future status
# addition can no longer silently widen it) and update_task_handler gained a target_agent
# ownership check, closing the class rather than just the parked-specific case.
# `routing-failed` is deliberately absent — an agent must not be able to terminally fail a
# task the dispatcher is still retrying. See vikunja#325 (task-queue-park-amend-2026-08 audit,
# originally accepted LOW, now fixed) and vikunja#324 (routing-failed vocabulary).
VALID_TRANSITIONS: dict[str, set[str]] = {
    "in-progress": {"approved"},  # agents must not claim unapproved tasks
    "completed": {"in-progress"},
    "failed": {"submitted", "pending-approval", "approved", "in-progress"},
}

# Operator-facing transitions (set_task_status). Broader than the agent-facing path but
# still audited and bounded. Standard moves below; `allow_override=True` additionally
# permits any non-terminal → any non-terminal (the "advance a missed task" feature).
# Terminal tasks are always immutable, even for operators.
OPERATOR_TRANSITIONS: dict[str, set[str]] = {
    "approved": {"submitted", "pending-approval"},
    "cancelled": NON_TERMINAL_STATUSES,  # any non-terminal task may be cancelled
    "parked": NON_TERMINAL_STATUSES - {"parked"},  # park any non-terminal task
}

# Set when a task is parked, recording the status to return to on unpark. Unparking is a
# non-terminal → non-terminal move, so it goes through the audited allow_override path.
PARKED_FROM_KEY = "parked_from"

# Statuses the submit-time auto-close (_auto_close_originating_task) may fire from.
# Deliberately narrower than "any non-terminal": `completed` is only reachable from
# `in-progress`, and each remaining non-terminal status is one the auto-close must not
# sweep — `parked` is a deliberate operator pause that closing would defeat,
# `submitted`/`pending-approval` have not been approved yet, and `routing-failed` is still
# being retried by the dispatcher.
AUTO_CLOSE_FROM_STATUSES = {"approved", "in-progress"}

# amend_task bounds. More than one or two amendments on a task is a signal to cancel and
# re-queue rather than accrete — these are a backstop, not a budget.
MAX_AMENDMENTS = 10
MAX_AMENDMENT_CHARS = 4096

# Only the agent that queued the task, or the operator, may amend it. The *target* agent
# must not be able to rewrite the instructions it was handed — the same trust boundary
# that already makes `cancelled` operator-only.
#
# THE SINGLE SOURCE OF TRUTH for the operator identity. server.py and auth.py import it
# from here; do not re-spell the literal in either. Every ownership check in this file
# reads `actor != owner and actor != OPERATOR_ACTOR`, and auth.py refuses to mint a token
# for this name — which is the assumption require_operator_surface rests on. Three
# independent spellings of one string, any of which could drift without an import error or
# a type error, and the failure is silent in both directions: strip the HTTP control routes
# of their exemption, or let a token be minted for an identity the handlers still exempt.
# (audit 2026-08-16, LOW)
#
# It lives here rather than in auth.py, which was the audit's suggestion, because this
# module is the domain layer and depends only on the standard library plus yaml. auth.py
# pulls in fastmcp, and homing the constant there would make the queue logic transitively
# depend on a server framework for a string. queue -> auth -> server has no cycle.
OPERATOR_ACTOR = "operator"

# ---------------------------------------------------------------------------
# Where a record lives
# ---------------------------------------------------------------------------
#
# The queue is three directories, not one. Until now only two of them were reachable:
# `get_task` searched the root then `archive/`, `list_tasks` searched the root, and
# `/queue/summary` counted the root. `dead-letters/` — written by the dispatcher when a
# task exhausts its routing retries — was addressable by no tool at all. Seventeen tasks
# accumulated there between 2026-05-29 and 2026-07-25, every one of them a security audit
# request, all seventeen carrying the identical `failed_reason`, and the only notice any of
# them ever got was a single Matrix message at the moment it was dropped.
# A failure path nothing can enumerate is a failure path nobody checks. (vikunja#557)
#
# The directory names are the dispatcher's — task_dispatcher.cli.DEAD_LETTER_DIR and
# ARCHIVE_DIR — and this module is the reader of what that writer produces.
ARCHIVE_DIRNAME = "archive"
DEAD_LETTER_DIRNAME = "dead-letters"

LOCATION_QUEUE = "queue"
LOCATION_ARCHIVE = "archive"
LOCATION_DEAD_LETTER = "dead-letters"

# Surfaced on every record `list_tasks` and `get_task` return, as `queue_location`. A
# caller must be able to tell a dead-lettered record from a live one without inspecting
# file paths — which it cannot see anyway, since `_path` is stripped on the way out.
QUEUE_LOCATION_KEY = "queue_location"

# context_refs validation: enforce absolute paths (must start with '/').
# Trust model: we do not restrict to a specific prefix allowlist — consumers
# are responsible for validating that dereferenced paths are accessible and safe.
# This is the accepted trust model for internal agent-to-agent coordination.
_CONTEXT_REF_MIN_LEN = 2  # at minimum "/<char>"


def _load_task_file(path: str) -> dict | None:
    """Load a single YAML task file. Returns None on parse or type error."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
        return None
    except Exception as e:
        logger.warning("Skipping unparseable task file %s: %s", path, e)
        return None


def _load_dir(directory: str, location: str, into: list[dict]) -> None:
    """Load every *.yml in one queue directory, tagging each record with its location."""
    for path in glob.glob(os.path.join(directory, "*.yml")):
        if path.endswith(".tmp"):
            continue
        task = _load_task_file(path)
        if task is not None:
            task["_path"] = path
            task["_location"] = location
            into.append(task)


def _load_all_tasks(
    queue_dir: str,
    include_archived: bool = False,
    include_dead_letters: bool = False,
) -> list[dict]:
    """
    Load all *.yml task files from queue_dir, skipping .tmp files.

    Attaches _path and _location to each task dict for internal use; both are stripped
    before anything is returned to a caller or written back to disk.

    Both opt-ins default off, and dead letters are deliberately the stricter of the two:
    an archived record is finished work, but a dead letter is *unfinished* work that no
    longer has a route, and it must never land in the `list_tasks` sweep an agent uses as
    its work list. See list_tasks_handler.
    """
    tasks: list[dict] = []

    _load_dir(queue_dir, LOCATION_QUEUE, tasks)
    if include_archived:
        _load_dir(os.path.join(queue_dir, ARCHIVE_DIRNAME), LOCATION_ARCHIVE, tasks)
    if include_dead_letters:
        _load_dir(os.path.join(queue_dir, DEAD_LETTER_DIRNAME), LOCATION_DEAD_LETTER, tasks)

    return tasks


def _public_task(task: dict) -> dict:
    """
    The caller-facing view of a loaded record: internal keys out, `queue_location` in.

    Every internal key is prefixed `_`, and this strips the whole class rather than
    naming them one at a time — the previous `k != "_path"` spelling would have leaked
    `_location` into every response the day it was added.
    """
    public = {k: v for k, v in task.items() if not k.startswith("_")}
    public[QUEUE_LOCATION_KEY] = task.get("_location", LOCATION_QUEUE)
    return public


def _write_task_atomic(path: str, data: dict) -> None:
    """Write task data atomically: write to .tmp then os.rename() to final path."""
    tmp = path + ".tmp"
    # Remove internal metadata before serialization — always use yaml.dump,
    # never string interpolation, to correctly escape user-supplied strings.
    #
    # Strips every `_`-prefixed key, not just `_path`. The handlers pop `_path` and then
    # hand the rest of the loaded dict straight here, so anything else the loader attaches
    # would be persisted into the YAML on the next transition — a load-time annotation
    # silently becoming a stored field.
    write_data = {k: v for k, v in data.items() if not k.startswith("_")}
    with open(tmp, "w") as f:
        yaml.dump(write_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.rename(tmp, path)


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_context_refs(context_refs: list) -> str | None:
    """Return an error string if any context_ref is invalid, else None."""
    for ref in context_refs:
        if not isinstance(ref, str) or not ref.startswith("/") or len(ref) < _CONTEXT_REF_MIN_LEN:
            return f"Invalid context_ref: {ref!r} — must be an absolute path starting with '/'"
    return None


@contextmanager
def _task_lock(queue_dir: str, task_id: str):
    """Acquire an exclusive per-task file lock for the duration of a load-modify-write."""
    lock_dir = os.path.join(queue_dir, ".locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"{task_id}.lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _find_dead_letter(queue_dir: str, task_id: str) -> dict | None:
    """The dead-lettered record for task_id, with _path/_location attached, or None."""
    records: list[dict] = []
    _load_dir(os.path.join(queue_dir, DEAD_LETTER_DIRNAME), LOCATION_DEAD_LETTER, records)
    return next((t for t in records if t.get("id") == task_id), None)


# The refusal a mutating handler gives for a dead-lettered task. It is a distinct message
# from `not found` on purpose: the mutating handlers deliberately do not load
# `dead-letters/` at all, which is what keeps a dead letter unreachable from update_task,
# set_task_status, park/unpark and amend — but that made every one of them answer
# `not found` for a record plainly on disk, which is the exact confusion this build exists
# to remove. The refusal names the state and the one door out of it.
DEAD_LETTER_REFUSAL = (
    "task is dead-lettered and cannot be mutated in place — requeue it first "
    "(operator only, POST /tasks/<id>/requeue)"
)


def _not_found(queue_dir: str, task_id: str) -> dict:
    """`not found`, refined to the dead-letter refusal when the record is dead-lettered."""
    if _find_dead_letter(queue_dir, task_id) is not None:
        return {"ok": False, "error": DEAD_LETTER_REFUSAL}
    return {"ok": False, "error": "not found"}


def count_dead_letters(queue_dir: str) -> int:
    """How many records sit in dead-letters/. Used by the /queue/summary route."""
    records: list[dict] = []
    _load_dir(os.path.join(queue_dir, DEAD_LETTER_DIRNAME), LOCATION_DEAD_LETTER, records)
    return len(records)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def submit_task_handler(
    source_agent: str,
    target_agent: str,
    task_type: str,
    summary: str,
    description: str,
    risk_level: str = "low",
    requires_approval: bool = False,
    priority: str = "normal",
    context_refs: list | None = None,
    ttl_days: int = 30,
    workflow_mode: str = "semi-auto",
    originating_task_id: str | None = None,
    queue_dir: str | None = None,
) -> dict:
    if context_refs is None:
        context_refs = []
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    if not source_agent or not source_agent.strip():
        return {"ok": False, "error": "source_agent must not be empty"}
    if not target_agent or not target_agent.strip():
        return {"ok": False, "error": "target_agent must not be empty"}
    if not summary or not summary.strip():
        return {"ok": False, "error": "summary must not be empty"}
    if task_type not in VALID_TASK_TYPES:
        return {
            "ok": False,
            "error": (
                f"Invalid task_type: {task_type!r}. Must be one of: {sorted(VALID_TASK_TYPES)}"
            ),
        }
    if risk_level not in VALID_RISK_LEVELS:
        return {
            "ok": False,
            "error": (
                f"Invalid risk_level: {risk_level!r}. Must be one of: {sorted(VALID_RISK_LEVELS)}"
            ),
        }
    if priority not in VALID_PRIORITIES:
        return {
            "ok": False,
            "error": f"Invalid priority: {priority!r}. Must be one of: {sorted(VALID_PRIORITIES)}",
        }
    if workflow_mode not in VALID_WORKFLOW_MODES:
        return {
            "ok": False,
            "error": (
                f"Invalid workflow_mode: {workflow_mode!r}. "
                f"Must be one of: {sorted(VALID_WORKFLOW_MODES)}"
            ),
        }
    if not isinstance(ttl_days, int) or ttl_days < 1:
        return {
            "ok": False,
            "error": f"Invalid ttl_days: {ttl_days!r}. Must be a positive integer.",
        }
    if originating_task_id is not None:
        try:
            uuid.UUID(originating_task_id)
        except ValueError:
            return {
                "ok": False,
                "error": f"Invalid originating_task_id: {originating_task_id!r} — must be a UUID",
            }

    if context_refs:
        err = _validate_context_refs(context_refs)
        if err:
            return {"ok": False, "error": err}

    task_id = str(uuid.uuid4())
    now = _now()
    slug = task_id[:8]
    filename = f"{now.strftime('%Y%m%d-%H%M%S')}-{slug}.yml"
    path = os.path.join(queue_dir, filename)

    payload: dict = {
        "description": description,
        "context_refs": context_refs,
        "priority": priority,
    }
    if originating_task_id is not None:
        payload["originating_task_id"] = originating_task_id

    # A self-terminal type is recorded and closed in the same write. The `submitted` entry
    # is kept and a `completed` entry appended rather than writing `completed` alone, for
    # the same reason the auto-close walks approved → in-progress → completed instead of
    # teleporting: the history should read as something that happened, not as a task that
    # sprang into existence finished.
    #
    # This does not go through VALID_TRANSITIONS (`completed` from `submitted` is not a
    # legal agent move, and must not become one). It is a distinct creation path, not a
    # transition — nothing here widens what `update_task` will accept.
    self_terminal = task_type in SELF_TERMINAL_TASK_TYPES
    approval_overridden = False
    if self_terminal:
        # Forced, not honoured. See SELF_TERMINAL_TASK_TYPES. Recorded in the note when the
        # caller actually asked for the opposite, so the override is visible in the trail
        # rather than being a silent disagreement between the call and the record.
        approval_overridden = bool(requires_approval)
        requires_approval = False

    task = {
        "id": task_id,
        "created": now,
        "source_agent": source_agent,
        "target_agent": target_agent,
        "task_type": task_type,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "workflow_mode": workflow_mode,
        "status": "completed" if self_terminal else "submitted",
        "summary": summary,
        "ttl_days": ttl_days,
        "payload": payload,
        "result": {
            "output": description if self_terminal else None,
            "completed_by": f"{source_agent} ({task_type})" if self_terminal else None,
            "completed_at": now if self_terminal else None,
        },
        "history": [
            {
                "timestamp": now,
                "status": "submitted",
                "actor": source_agent,
                "note": "Task submitted via task-queue-mcp",
            }
        ],
        "retry_policy": {
            "next_retry_at": None,
            "retry_count": 0,
        },
    }

    if self_terminal:
        note = "Notification delivered — no session required"
        if approval_overridden:
            note = f"{note} (requires_approval=True ignored: {task_type} is self-terminal)"
        task["history"].append(
            {
                "timestamp": now,
                "status": "completed",
                "actor": source_agent,
                "note": note,
            }
        )

    _write_task_atomic(path, task)

    result = {"ok": True, "task_id": task_id, "filename": filename}
    if self_terminal:
        result["status"] = "completed"
        result["self_terminal"] = True
        logger.info(
            "task.notify_delivered id=%s %s→%s summary=%r",
            task_id[:8],
            source_agent,
            target_agent,
            summary,
        )

    # Fail-safe close of the parent request task. Runs after the write, and cannot fail it.
    if originating_task_id is not None:
        closed = _auto_close_originating_task(
            originating_task_id=originating_task_id,
            source_agent=source_agent,
            target_agent=target_agent,
            new_task_id=task_id,
            queue_dir=queue_dir,
            new_task_summary=summary,
        )
        if closed:
            result["auto_closed_task_id"] = closed

    return result


def list_tasks_handler(
    target_agent: str | None = None,
    source_agent: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    include_archived: bool = False,
    include_dead_letters: bool = False,
    limit: int = 20,
    queue_dir: str | None = None,
) -> list:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    limit = max(1, min(limit, 200))

    # Reject an unrecognised status rather than filtering on it and returning [].
    #
    # This used to be permissive, and the silence cost real work: writer/CLAUDE.md swept its
    # queue with status="pending" — not a status this server has ever had — so the call
    # matched nothing and returned an empty list for months, indistinguishable from "no work
    # for you". Raising is deliberate: an empty list is a legitimate answer to a well-formed
    # question, so the only way to distinguish a typo from an empty queue is to refuse the
    # typo. FastMCP surfaces the message verbatim to the caller.
    status_filter = None
    if status:
        requested = [s.strip() for s in status.split(",") if s.strip()]
        invalid = [s for s in requested if s not in VALID_STATUSES]
        if invalid or not requested:
            raise ValueError(
                f"Invalid status filter: {sorted(invalid) or status!r}. "
                f"Must be one of: {sorted(VALID_STATUSES)} "
                f"(single value or comma-separated)."
            )
        status_filter = set(requested)

    # Dead letters are opt-in and OFF by default, mirroring include_archived. A dead
    # letter is not actionable work: every agent's work sweep is a `list_tasks` call, and
    # folding seventeen permanently-unroutable records into it would hand each agent a
    # backlog it cannot act on. Visibility is the point, not re-delivery.
    tasks = _load_all_tasks(
        queue_dir,
        include_archived=include_archived,
        include_dead_letters=include_dead_letters,
    )

    now = _now()
    filtered = []
    for task in tasks:
        # TTL filter: skip tasks past their TTL. TTL enforcement is authoritative in the
        # dispatcher, but we filter here too so agents don't act on stale items if the
        # dispatcher falls behind.
        #
        # NON-TERMINAL TASKS ARE EXEMPT (vikunja#395). This used to exempt only `parked`,
        # which meant open work — submitted, approved, in-progress, routing-failed —
        # silently disappeared from every listing once it passed ttl_days, while still
        # sitting on disk waiting for someone. That is not a stale-item guard, it is a
        # blind spot, and it is how a queue sweep found 17 stranded tasks after this same
        # tool reported 13: the four oldest had aged out of the listing that was used to
        # count them.
        #
        # The original reasoning still holds for finished work, so terminal records still
        # age out of the default view. But nothing that is still someone's responsibility
        # should be hidden by a clock — an agent handed a stale open task can judge it,
        # whereas nobody can act on a task they cannot see.
        # DEAD LETTERS ARE EXEMPT TOO, for the same reason parked and open work are. A
        # dead-lettered task carries the status the dispatcher last wrote — `failed`,
        # which is terminal — so without this every one of the seventeen would age out of
        # the very listing added to reveal them: the newest is past its ttl_days, and
        # `include_dead_letters=True` would have returned an empty list. Ageing out is for
        # finished work. A dead letter is unfinished work with no route, and a clock must
        # not be what hides it.
        created = task.get("created")
        ttl_days = task.get("ttl_days", 30)
        if (
            task.get("_location") != LOCATION_DEAD_LETTER
            and task.get("status") not in NON_TERMINAL_STATUSES
            and created
            and isinstance(created, datetime)
            and now > created + timedelta(days=ttl_days)
        ):
            continue

        if target_agent and task.get("target_agent") != target_agent:
            continue
        if source_agent and task.get("source_agent") != source_agent:
            continue
        if status_filter and task.get("status") not in status_filter:
            continue
        if task_type and task.get("task_type") != task_type:
            continue

        filtered.append(task)

    # Created descending, but DEAD LETTERS FIRST when they were asked for.
    #
    # This is not a cosmetic preference. A dead letter is by construction among the oldest
    # records in the queue — it got there by exhausting retries — so under a plain
    # created-descending sort all seventeen land at the very bottom, behind several hundred
    # live tasks, and `filtered[:limit]` discards every one of them. Verified against the
    # live queue while building this: `include_dead_letters=True, limit=200` returned 200
    # rows and zero dead letters. A flag whose whole purpose is to reveal them, returning
    # none of them on the only queue that has any, is the same silence in a new shape.
    #
    # Nobody passes this flag except to audit the failure path, so the thing asked for is
    # the thing shown first. The ordering within each group is unchanged.
    def _sort_key(t: dict) -> tuple[int, datetime]:
        c = t.get("created")
        created = c if isinstance(c, datetime) else datetime.min.replace(tzinfo=UTC)
        return (1 if t.get("_location") == LOCATION_DEAD_LETTER else 0, created)

    filtered.sort(key=_sort_key, reverse=True)

    return [_public_task(t) for t in filtered[:limit]]


def get_task_handler(task_id: str, queue_dir: str | None = None) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    # Search main queue, then archive/, then dead-letters/.
    #
    # Dead letters are included here with no opt-in, unlike in list_tasks: asking for a
    # task by id is not a work sweep, it is someone holding a specific id and wanting to
    # know what became of it. Answering `not found` for a record sitting on disk is the
    # bug — a dropped audit request looked identical to an id that never existed. The
    # returned record keeps its `failed_reason` block and carries `queue_location`, so a
    # caller can tell a dead letter from live work without inspecting paths.
    tasks = _load_all_tasks(queue_dir, include_archived=True, include_dead_letters=True)
    for task in tasks:
        if task.get("id") == task_id:
            return _public_task(task)

    return {"ok": False, "error": "not found"}


def update_task_handler(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    output: str | None = None,
    queue_dir: str | None = None,
    on_behalf_of: str | None = None,
) -> dict:
    """
    Transition a task and append a history entry.

    on_behalf_of is the audited operator sweep path, and exists because closing the MCP
    path closed the dishonest one. Agents used to tidy up another agent's stranded task by
    passing that agent's name as `actor` — 17 were swept that way in the release before
    this one, honestly annotated, and only possible because `actor` was a free string. Once
    `actor` is derived from a bearer token that route is gone, and nothing replaces it:
    set_task_status cannot make terminal transitions, and update_task now demands the
    resolved identity. Without this, every future stray needs the operator personally.

    So the operator may close another agent's task, but must say whose it is, and the
    history records both — `actor: operator` alongside `on_behalf_of: <agent>`. Reachable
    only where OPERATOR_ACTOR can be asserted, which after this release is the
    shared-secret-gated control routes and nowhere else.
    """
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    if on_behalf_of is not None and actor != OPERATOR_ACTOR:
        return {
            "ok": False,
            "error": (
                f"on_behalf_of is reserved for the {OPERATOR_ACTOR!r} actor; "
                f"{actor!r} may not act for another agent."
            ),
        }

    valid_update_statuses = {"in-progress", "completed", "failed"}
    if status not in valid_update_statuses:
        return {
            "ok": False,
            "error": (
                f"Invalid status: {status!r}. update_task accepts: {sorted(valid_update_statuses)}"
            ),
        }

    with _task_lock(queue_dir, task_id):
        tasks = _load_all_tasks(queue_dir, include_archived=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return _not_found(queue_dir, task_id)

        if task.get("_location") == LOCATION_ARCHIVE:
            return {"ok": False, "error": "task is archived and cannot be updated"}

        current_status = task.get("status")

        if current_status in TERMINAL_STATUSES:
            return {
                "ok": False,
                "error": f"Task is in terminal status {current_status!r} and cannot be updated",
            }

        target_agent = task.get("target_agent")
        if actor != target_agent and actor != OPERATOR_ACTOR:
            return {
                "ok": False,
                "error": (
                    f"actor {actor!r} is not the target agent for this task "
                    f"({target_agent!r}) and may not update it."
                ),
            }

        # A sweep names the agent it is acting for, and that name has to be right — an
        # operator closing the wrong task should be told, not have the mistake recorded as
        # deliberate. Checked here, under the lock, against the task actually being written.
        if on_behalf_of is not None and on_behalf_of != target_agent:
            return {
                "ok": False,
                "error": (
                    f"on_behalf_of {on_behalf_of!r} is not the target agent for this task "
                    f"({target_agent!r})."
                ),
            }

        allowed_from = VALID_TRANSITIONS.get(status, set())
        if current_status not in allowed_from:
            return {
                "ok": False,
                "error": (
                    f"Invalid transition: {current_status!r} → {status!r}. "
                    f"Allowed from: {sorted(allowed_from)}"
                ),
            }

        now = _now()
        task["status"] = status

        if status in {"completed", "failed"}:
            if task.get("result") is None:
                task["result"] = {}
            task["result"]["completed_by"] = actor
            task["result"]["completed_at"] = now
            if output is not None:
                task["result"]["output"] = output

        history_entry = {
            "timestamp": now,
            "status": status,
            "actor": actor,
            "note": note,
        }
        if on_behalf_of is not None:
            history_entry["on_behalf_of"] = on_behalf_of
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # retry_policy is owned by the task-dispatcher — never modify it
        path = task.pop("_path")
        _write_task_atomic(path, task)

    logger.info(
        "task.transition id=%s %s→%s actor=%s%s",
        task_id[:8],
        current_status,
        status,
        actor,
        f" on_behalf_of={on_behalf_of}" if on_behalf_of else "",
    )
    return {"ok": True, "task_id": task_id}


def _auto_close_originating_task(
    originating_task_id: str,
    source_agent: str,
    target_agent: str,
    new_task_id: str,
    queue_dir: str,
    new_task_summary: str = "",
) -> str | None:
    """
    Fail-safe: close a request task when its return task is submitted.

    The problem it solves: the agent that does the work is not the agent that closes the
    record of it. A build agent submits an audit request targeting `security`; security
    files the audit and submits a return task; nobody closes the request, because the build
    agent is not its target agent and may not update it. 14 audit tasks accumulated that way
    between 2026-07-19 and 2026-08-15.

    Fires only on the *return shape*: the new task must be addressed back to whoever asked.

        parent.target_agent == new.source_agent    # I did the parent's work
        parent.source_agent == new.target_agent    # and I am answering the asker

    BOTH are required, and the second one is not optional bookkeeping — it is the fix for a
    bug this feature shipped with. `originating_task_id` is overloaded: it means "inherit
    workflow_mode from this parent" on a *forward* request as much as "this is the return
    for that request" on a return. `shared-build-pre-audit` Step 4 has always told the build
    agent to pass its own build task when submitting the audit request, purely for
    workflow_mode inheritance. Under the first condition alone that looks identical to a
    return — parent build task targets developer, developer submits — so the very first
    audit request filed after v0.6.0 shipped auto-closed its own in-flight build task, and
    terminal tasks are immutable, so it could not be reopened. (2026-08-16, live.)

    Checking the pair distinguishes them: a genuine return is symmetric (audit task
    developer→security, return security→developer), while a forward request is not
    (build task research→developer, request developer→security — `research != security`).

    The first condition is also what keeps this from being a cross-agent close primitive —
    agent A naming agent B's task as its parent must not close it. It is checked here
    explicitly rather than relying on update_task_handler's ownership check, which also
    admits OPERATOR_ACTOR: a caller submitting as source_agent="operator" would otherwise be
    able to close anyone's task.

    Deliberately narrower than "any non-terminal parent" (see AUTO_CLOSE_FROM_STATUSES).

    Returns the parent's id if it was closed, else None. Never raises — the auto-close is a
    convenience, and a failure here must not fail the submit that triggered it.
    """
    try:
        tasks = _load_all_tasks(queue_dir, include_archived=True)
        parent = next((t for t in tasks if t.get("id") == originating_task_id), None)

        if parent is None:
            logger.warning(
                "auto-close skipped: originating task %s not found", originating_task_id[:8]
            )
            return None
        if parent.get("_location") == LOCATION_ARCHIVE:
            return None

        parent_status = parent.get("status")
        if parent_status not in AUTO_CLOSE_FROM_STATUSES:
            return None

        # The return shape, both halves. See the docstring — dropping the second check is
        # what closed an in-flight build task on 2026-08-16.
        if parent.get("target_agent") != source_agent:
            return None
        if parent.get("source_agent") != target_agent:
            logger.info(
                "auto-close skipped: %s is a forward request, not a return "
                "(parent asked by %r, this task is addressed to %r)",
                originating_task_id[:8],
                parent.get("source_agent"),
                target_agent,
            )
            return None

        # Carry the return task's summary into the note. The auto-close always wins the
        # race against the answering agent's own explicit close — it fires during
        # submit_task of the return task, which necessarily precedes that call — so this
        # note is what the history actually ends up recording, and the agent's own wording
        # never lands. Without the summary the trail reads "auto-closed: return task
        # <uuid> submitted", which says a reply happened but not what it said.
        note = f"auto-closed: return task {new_task_id} submitted"
        if new_task_summary:
            note = f"{note} — {new_task_summary}"

        # Walk approved → in-progress first. `completed` is only reachable from
        # `in-progress` (VALID_TRANSITIONS), and going through the real transition rather
        # than writing the status directly keeps the history legible: the record shows the
        # task was claimed and then closed, not teleported.
        if parent_status == "approved":
            claim = update_task_handler(
                task_id=originating_task_id,
                status="in-progress",
                actor=source_agent,
                note=note,
                queue_dir=queue_dir,
            )
            if not claim.get("ok"):
                logger.warning(
                    "auto-close skipped: could not claim %s — %s",
                    originating_task_id[:8],
                    claim.get("error"),
                )
                return None

        result = update_task_handler(
            task_id=originating_task_id,
            status="completed",
            actor=source_agent,
            note=note,
            queue_dir=queue_dir,
        )
        if not result.get("ok"):
            logger.warning(
                "auto-close failed for %s — %s", originating_task_id[:8], result.get("error")
            )
            return None

        logger.info(
            "task.auto_close id=%s actor=%s trigger=%s",
            originating_task_id[:8],
            source_agent,
            new_task_id[:8],
        )
        return originating_task_id
    except Exception:
        logger.warning(
            "auto-close raised for originating task %s — submit unaffected",
            originating_task_id[:8],
            exc_info=True,
        )
        return None


def set_task_status_handler(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    allow_override: bool = False,
    queue_dir: str | None = None,
    enforce_ownership: bool = False,
) -> dict:
    """
    Operator-facing status change. Broader than update_task but audited and bounded:

      - submitted/pending-approval → approved
      - any non-terminal          → cancelled
      - any non-terminal          → parked
      - any non-terminal → any non-terminal (only with allow_override=True; the
        deliberate "advance a missed task" override — a non-empty note is required)
      - any *unrecognised* status → any valid status (only with allow_override=True and
        a non-empty note; the repair path for records written outside this server)

    Parking records the prior status in `parked_from` so unpark_task can restore it; the
    key is cleared on the way out. Terminal tasks (completed/failed/cancelled) are
    immutable. Archived tasks cannot be mutated. Every change appends a history entry.

    enforce_ownership restricts the change to the task's own target_agent (or the
    operator). It is off by default because the control routes are an operator surface and
    the direct handler calls predate any ownership rule; the MCP park/unpark tools pass it
    on, which is what lets an agent pause its own work without being able to pause anyone
    else's. The remaining transitions this handler serves stay operator-only, so they never
    reach it with it set.
    """
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    if status not in VALID_STATUSES:
        return {
            "ok": False,
            "error": f"Invalid status: {status!r}. Must be one of: {sorted(VALID_STATUSES)}",
        }

    if not actor or not actor.strip():
        return {"ok": False, "error": "actor must not be empty"}

    with _task_lock(queue_dir, task_id):
        tasks = _load_all_tasks(queue_dir, include_archived=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return _not_found(queue_dir, task_id)

        if task.get("_location") == LOCATION_ARCHIVE:
            return {"ok": False, "error": "task is archived and cannot be updated"}

        current_status = task.get("status")

        if current_status in TERMINAL_STATUSES:
            return {
                "ok": False,
                "error": f"Task is in terminal status {current_status!r} and cannot be updated",
            }

        if enforce_ownership:
            owner = task.get("target_agent")
            if actor != owner and actor != OPERATOR_ACTOR:
                return {
                    "ok": False,
                    "error": (
                        f"actor {actor!r} is not the target agent for this task "
                        f"({owner!r}) and may not park or unpark it."
                    ),
                }

        standard_ok = current_status in OPERATOR_TRANSITIONS.get(status, set())
        override_ok = (
            allow_override
            and status in NON_TERMINAL_STATUSES
            and current_status in NON_TERMINAL_STATUSES
        )
        # Repair path: a record whose current status is not in our vocabulary at all (e.g.
        # `complete` vs `completed`, or the dispatcher's `routing-failed`) is unreachable by
        # every other branch — standard_ok needs it in an OPERATOR_TRANSITIONS set and
        # override_ok needs it in NON_TERMINAL_STATUSES. Written by a direct-YAML writer,
        # such a task is otherwise permanently stuck. This is the narrowest unsticking that
        # loosens no legitimate transition: it requires an explicit override plus a note,
        # and only ever moves *out of* an invalid status.
        repair_ok = allow_override and current_status not in VALID_STATUSES

        if not (standard_ok or override_ok or repair_ok):
            error = (
                f"Invalid operator transition: {current_status!r} → {status!r}. "
                f"Standard targets: approved (from submitted/pending-approval), "
                f"cancelled or parked (from any non-terminal). For other non-terminal moves "
                f"pass allow_override=True."
            )
            # If this exact transition is one the agent-facing update_task tool accepts
            # (e.g. in-progress→completed, approved→in-progress, or →failed), point the
            # caller there. set_task_status is operator-only and structurally cannot reach
            # terminal statuses even with allow_override — the forward
            # in-progress→completed path lives on update_task. Naming it here is what
            # actually unblocks agents that hit this wall instead of retrying override.
            if current_status in VALID_TRANSITIONS.get(status, set()):
                error += (
                    " This transition is available via update_task (agent-facing) — "
                    "set_task_status is operator-only and cannot reach terminal statuses "
                    "via override."
                )
            return {"ok": False, "error": error}

        if (override_ok or repair_ok) and not standard_ok and not (note and note.strip()):
            return {
                "ok": False,
                "error": "an override transition requires a non-empty note for the audit trail",
            }

        now = _now()
        task["status"] = status

        # Parking records where to return to; leaving parked clears the marker so it can
        # never go stale and point at a status the task is no longer in.
        if status == "parked":
            if current_status != "parked":
                task[PARKED_FROM_KEY] = current_status
        else:
            task.pop(PARKED_FROM_KEY, None)

        if status in TERMINAL_STATUSES:
            if task.get("result") is None:
                task["result"] = {}
            task["result"]["completed_by"] = actor
            task["result"]["completed_at"] = now

        history_entry = {
            "timestamp": now,
            "status": status,
            "actor": actor,
            "note": note,
        }
        if (override_ok or repair_ok) and not standard_ok:
            history_entry["override"] = True
        if repair_ok:
            history_entry["repaired_from"] = current_status
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # retry_policy is owned by the task-dispatcher — never modify it
        path = task.pop("_path")
        _write_task_atomic(path, task)

    logger.info(
        "task.operator_transition id=%s %s→%s actor=%s override=%s",
        task_id[:8],
        current_status,
        status,
        actor,
        (override_ok or repair_ok) and not standard_ok,
    )
    return {"ok": True, "task_id": task_id}


def cancel_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    queue_dir: str | None = None,
) -> dict:
    """
    Cancel a task: a graceful, audited terminal state for stale/unwanted tasks.
    Recoverable as a record (the YAML stays on disk) but, like any terminal status,
    cannot be transitioned out of. Thin wrapper over set_task_status_handler.
    """
    return set_task_status_handler(
        task_id=task_id,
        status="cancelled",
        actor=actor,
        note=note or "Cancelled by operator",
        queue_dir=queue_dir,
    )


def park_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    queue_dir: str | None = None,
    enforce_ownership: bool = False,
) -> dict:
    """
    Park a task: pause it without hiding it. The YAML stays exactly where it is and the
    task keeps appearing in list_tasks — only its status changes, so the dispatcher's
    pickup loops (which match `submitted` and `routing-failed`) skip it and nothing sweeps
    it at TTL. The prior status is recorded in `parked_from` for unpark_task.

    Thin wrapper over set_task_status_handler, same as cancel_task.
    """
    return set_task_status_handler(
        task_id=task_id,
        status="parked",
        actor=actor,
        note=note or "Parked by operator",
        queue_dir=queue_dir,
        enforce_ownership=enforce_ownership,
    )


def unpark_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    status: str | None = None,
    queue_dir: str | None = None,
    enforce_ownership: bool = False,
) -> dict:
    """
    Unpark a task, returning it to the status it was parked from. Pass `status` to send it
    somewhere else instead. Errors if the task is not parked, or if it carries no
    `parked_from` (a task parked before this field existed, or by a direct-YAML writer) and
    no explicit status was given.

    SECURITY[accepted]: the target status is resolved from a read taken *outside* the write
    lock, because set_task_status_handler acquires the same non-reentrant fcntl lock and
    holding it across both would deadlock. An illegal transition still cannot land — that
    handler re-reads `current_status` under the lock and re-validates against it. The
    residual race is narrower: if a second operator re-parks or unparks this task between
    our read and that call, the stale `target` can produce a redundant-but-valid transition
    plus a duplicate history entry. An audit-trail nuisance, not a state-integrity or
    authorization bypass. Accepted given park/unpark is a human clicking a button, not
    concurrent automation. Closing it fully needs a reentrant lock or a
    set_task_status_handler that accepts a pre-loaded task.
    (task-queue-park-amend-2026-08 audit, LOW)
    """
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    # Resolve the target before taking the write lock — set_task_status_handler acquires
    # the same (non-reentrant) per-task lock.
    tasks = _load_all_tasks(queue_dir, include_archived=True)
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if task is None:
        return _not_found(queue_dir, task_id)
    if task.get("status") != "parked":
        return {"ok": False, "error": f"task is not parked (status: {task.get('status')!r})"}

    target = status or task.get(PARKED_FROM_KEY)
    if not target:
        return {
            "ok": False,
            "error": (
                "task has no recorded parked_from status — pass an explicit status to "
                f"unpark it. Valid statuses: {sorted(NON_TERMINAL_STATUSES)}"
            ),
        }

    return set_task_status_handler(
        task_id=task_id,
        status=target,
        actor=actor,
        note=note or f"Unparked by operator (→ {target})",
        allow_override=True,
        queue_dir=queue_dir,
        enforce_ownership=enforce_ownership,
    )


def amend_task_handler(
    task_id: str,
    amendment: str,
    actor: str,
    reason: str = "",
    queue_dir: str | None = None,
) -> dict:
    """
    Append an amendment to a queued task. Append-only by construction: the original
    `payload.description` is never mutated, so the record of what the task was originally
    asked to do survives every correction.

    Authorization: the task's `source_agent` or the operator. The *target* agent is
    rejected — it must not be able to rewrite the instructions it was handed.

    Amending an in-progress task is permitted (it is the case that matters most), but the
    response carries `agent_may_have_started` so the caller knows the agent may already
    have read the original and needs telling out of band.
    """
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    if not actor or not actor.strip():
        return {"ok": False, "error": "actor must not be empty"}

    if not amendment or not amendment.strip():
        return {"ok": False, "error": "amendment must not be empty"}

    if len(amendment) > MAX_AMENDMENT_CHARS:
        return {
            "ok": False,
            "error": (
                f"amendment is {len(amendment)} chars, over the {MAX_AMENDMENT_CHARS} limit. "
                f"Cancel and re-queue the task rather than accreting a large correction."
            ),
        }

    with _task_lock(queue_dir, task_id):
        tasks = _load_all_tasks(queue_dir, include_archived=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return _not_found(queue_dir, task_id)

        if task.get("_location") == LOCATION_ARCHIVE:
            return {"ok": False, "error": "task is archived and cannot be amended"}

        current_status = task.get("status")
        if current_status in TERMINAL_STATUSES:
            return {
                "ok": False,
                "error": f"Task is in terminal status {current_status!r} and cannot be amended",
            }

        source_agent = task.get("source_agent")
        if actor != source_agent and actor != OPERATOR_ACTOR:
            return {
                "ok": False,
                "error": (
                    f"actor {actor!r} may not amend this task. Only its source_agent "
                    f"({source_agent!r}) or {OPERATOR_ACTOR!r} may amend — the target agent "
                    f"must not rewrite its own instructions."
                ),
            }

        payload = task.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            task["payload"] = payload

        amendments = payload.get("amendments")
        if not isinstance(amendments, list):
            amendments = []
            payload["amendments"] = amendments

        if len(amendments) >= MAX_AMENDMENTS:
            return {
                "ok": False,
                "error": (
                    f"task already has {len(amendments)} amendments (limit {MAX_AMENDMENTS}). "
                    f"Cancel and re-queue rather than accreting further."
                ),
            }

        now = _now()
        amendments.append(
            {
                "timestamp": now,
                "actor": actor,
                "reason": reason,
                "text": amendment,
            }
        )

        history_entry = {
            "timestamp": now,
            "status": current_status,
            "actor": actor,
            "note": reason or "Task amended",
            "action": "amend",
        }
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # retry_policy is owned by the task-dispatcher — never modify it
        path = task.pop("_path")
        _write_task_atomic(path, task)

    logger.info(
        "task.amend id=%s actor=%s count=%d status=%s",
        task_id[:8],
        actor,
        len(amendments),
        current_status,
    )
    return {
        "ok": True,
        "task_id": task_id,
        "amendment_count": len(amendments),
        "agent_may_have_started": current_status == "in-progress",
    }


def requeue_dead_letter_handler(
    task_id: str,
    actor: str,
    note: str = "",
    queue_dir: str | None = None,
) -> dict:
    """
    Move a dead-lettered task back into the active queue at `submitted`.

    OPERATOR ONLY. This is the one path in the whole module that walks a record out of a
    terminal status, and it exists because the alternative — a dropped task being
    unrecoverable except by hand-editing YAML — is what let seventeen of them sit for
    three months. Resurrecting work is an operator judgement in the same way cancelling it
    is; an agent must not be able to bring back its own dropped request, or a routing bug
    becomes an agent-driven retry loop with no ceiling. The gate is enforced one level up,
    in server.py, by the same `require_operator_surface` that gates set_task_status.

    The terminal-immutability rule is not weakened by this. It is scoped to the
    dead-letter directory and nowhere else: the record is looked up ONLY under
    `dead-letters/`, so a `failed` task in the queue root or in `archive/` is not
    reachable here however its id is spelled. A dead letter's `failed` status is the
    dispatcher's record of exhausting its retries, not an agent's judgement that the work
    is over.

    What changes: status → `submitted`, `failed_reason` dropped, `retry_policy` reset to
    a fresh `{next_retry_at: None, retry_count: 0}` — the whole block, since leaving a
    `last_failure_reason` and a long-past `next_retry_at` behind would describe a failure
    that is no longer this task's state. `created` is NOT refreshed: when the work was
    first asked for is the record, and rewriting it to make the age look better is exactly
    the thing that made this backlog invisible. `alert_state` is left alone — the
    dispatcher owns it.

    NOTE the root cause is not fixed by this. Requeueing one of the seventeen tasks that
    were dropped for `Invalid or missing build_name in payload: 'unknown'` sends it back
    through the same routing that rejected it, and it will dead-letter again after three
    retries. That bug is vikunja#63/#169. Amend the task's payload first, or expect it
    back.

    Returns {ok, task_id, filename, requeued_from} or {ok: false, error}.
    """
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    if not actor or not actor.strip():
        return {"ok": False, "error": "actor must not be empty"}

    with _task_lock(queue_dir, task_id):
        task = _find_dead_letter(queue_dir, task_id)
        if task is None:
            return {"ok": False, "error": "not found"}

        src = task.pop("_path")
        dest = os.path.join(queue_dir, os.path.basename(src))

        # A live file under the same name means some other record already occupies this
        # slot in the queue root. Overwriting it would destroy live work to recover dead
        # work, so refuse and let an operator look.
        if os.path.exists(dest):
            return {
                "ok": False,
                "error": (
                    f"cannot requeue: {os.path.basename(dest)} already exists in the "
                    f"queue root. Resolve the collision by hand."
                ),
            }

        now = _now()
        previous_status = task.get("status")
        failed_reason = task.pop("failed_reason", None)
        task["status"] = "submitted"
        task["retry_policy"] = {"next_retry_at": None, "retry_count": 0}

        history_entry = {
            "timestamp": now,
            "status": "submitted",
            "actor": actor,
            "note": note or "Requeued from dead-letters",
            "action": "requeue",
            "requeued_from": LOCATION_DEAD_LETTER,
        }
        if isinstance(failed_reason, dict) and failed_reason.get("reason"):
            # The reason it was dropped survives its own field being cleared. Without
            # this, a requeued task carries no trace of why it was ever dead, and the
            # second drop reads as the first.
            history_entry["cleared_failed_reason"] = failed_reason["reason"]
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # Write the destination first, then unlink the source: a crash between the two
        # leaves the task visible in both places, which an operator can see and fix. The
        # reverse order can lose it entirely. Same order the dispatcher uses on the way in.
        _write_task_atomic(dest, task)
        os.remove(src)

    logger.info(
        "task.requeue id=%s %s→submitted actor=%s from=%s",
        task_id[:8],
        previous_status,
        actor,
        LOCATION_DEAD_LETTER,
    )
    return {
        "ok": True,
        "task_id": task_id,
        "filename": os.path.basename(dest),
        "requeued_from": LOCATION_DEAD_LETTER,
    }
