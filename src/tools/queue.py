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
VALID_WORKFLOW_MODES = {"semi-auto", "auto"}
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
OPERATOR_ACTOR = "operator"

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


def _load_all_tasks(queue_dir: str, include_archived: bool = False) -> list[dict]:
    """
    Load all *.yml task files from queue_dir, skipping .tmp files.
    Attaches _path to each task dict for internal use (stripped before returning to callers).
    """
    tasks = []

    for path in glob.glob(os.path.join(queue_dir, "*.yml")):
        if path.endswith(".tmp"):
            continue
        task = _load_task_file(path)
        if task is not None:
            task["_path"] = path
            tasks.append(task)

    if include_archived:
        for path in glob.glob(os.path.join(queue_dir, "archive", "*.yml")):
            if path.endswith(".tmp"):
                continue
            task = _load_task_file(path)
            if task is not None:
                task["_path"] = path
                tasks.append(task)

    return tasks


def _write_task_atomic(path: str, data: dict) -> None:
    """Write task data atomically: write to .tmp then os.rename() to final path."""
    tmp = path + ".tmp"
    # Remove internal metadata before serialization — always use yaml.dump,
    # never string interpolation, to correctly escape user-supplied strings.
    write_data = {k: v for k, v in data.items() if k != "_path"}
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

    task = {
        "id": task_id,
        "created": now,
        "source_agent": source_agent,
        "target_agent": target_agent,
        "task_type": task_type,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "workflow_mode": workflow_mode,
        "status": "submitted",
        "summary": summary,
        "ttl_days": ttl_days,
        "payload": payload,
        "result": {
            "output": None,
            "completed_by": None,
            "completed_at": None,
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

    _write_task_atomic(path, task)

    result = {"ok": True, "task_id": task_id, "filename": filename}

    # Fail-safe close of the parent request task. Runs after the write, and cannot fail it.
    if originating_task_id is not None:
        closed = _auto_close_originating_task(
            originating_task_id=originating_task_id,
            source_agent=source_agent,
            target_agent=target_agent,
            new_task_id=task_id,
            queue_dir=queue_dir,
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

    tasks = _load_all_tasks(queue_dir, include_archived=include_archived)

    now = _now()
    filtered = []
    for task in tasks:
        # TTL filter: skip tasks past their TTL. TTL enforcement is authoritative in the
        # dispatcher, but we filter here too so agents don't act on stale items if the
        # dispatcher falls behind.
        #
        # Parked tasks are exempt. Parking is a deliberate "pause this, I'll come back to
        # it" — a parked task silently vanishing at TTL would defeat the entire point of
        # the status, which exists to give long-lived bookmarks a vocabulary.
        created = task.get("created")
        ttl_days = task.get("ttl_days", 30)
        if (
            task.get("status") != "parked"
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

    def _sort_key(t: dict) -> datetime:
        c = t.get("created")
        if isinstance(c, datetime):
            return c
        return datetime.min.replace(tzinfo=UTC)

    filtered.sort(key=_sort_key, reverse=True)

    return [{k: v for k, v in t.items() if k != "_path"} for t in filtered[:limit]]


def get_task_handler(task_id: str, queue_dir: str | None = None) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    # Search main queue, then archive/
    tasks = _load_all_tasks(queue_dir, include_archived=True)
    for task in tasks:
        if task.get("id") == task_id:
            return {k: v for k, v in task.items() if k != "_path"}

    return {"ok": False, "error": "not found"}


def update_task_handler(
    task_id: str,
    status: str,
    actor: str,
    note: str = "",
    output: str | None = None,
    queue_dir: str | None = None,
) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

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
            return {"ok": False, "error": "not found"}

        if "archive" in task.get("_path", ""):
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
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # retry_policy is owned by the task-dispatcher — never modify it
        path = task.pop("_path")
        _write_task_atomic(path, task)

    logger.info("task.transition id=%s %s→%s actor=%s", task_id[:8], current_status, status, actor)
    return {"ok": True, "task_id": task_id}


def _auto_close_originating_task(
    originating_task_id: str,
    source_agent: str,
    target_agent: str,
    new_task_id: str,
    queue_dir: str,
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
        if "archive" in parent.get("_path", ""):
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

        note = f"auto-closed: return task {new_task_id} submitted"

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
            return {"ok": False, "error": "not found"}

        if "archive" in task.get("_path", ""):
            return {"ok": False, "error": "task is archived and cannot be updated"}

        current_status = task.get("status")

        if current_status in TERMINAL_STATUSES:
            return {
                "ok": False,
                "error": f"Task is in terminal status {current_status!r} and cannot be updated",
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
    )


def unpark_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    status: str | None = None,
    queue_dir: str | None = None,
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
        return {"ok": False, "error": "not found"}
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
            return {"ok": False, "error": "not found"}

        if "archive" in task.get("_path", ""):
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
