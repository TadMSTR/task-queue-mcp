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
VALID_TASK_TYPES = {"build", "deploy", "fix", "research", "review", "audit", "notify"}
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
    return {"ok": True, "task_id": task_id, "filename": filename}


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
    tasks = _load_all_tasks(queue_dir, include_archived=include_archived)

    status_filter = None
    if status:
        status_filter = {s.strip() for s in status.split(",")}

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
