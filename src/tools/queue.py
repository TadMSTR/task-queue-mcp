import fcntl
import glob
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import yaml

logger = logging.getLogger(__name__)

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_STATUSES = {
    "submitted",
    "approved",
    "pending-approval",
    "in-progress",
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
VALID_TRANSITIONS: dict[str, set[str]] = {
    "in-progress": {"approved"},  # agents must not claim unapproved tasks
    "completed": {"in-progress"},
    "failed": NON_TERMINAL_STATUSES,
}

# Operator-facing transitions (set_task_status). Broader than the agent-facing path but
# still audited and bounded. Standard moves below; `allow_override=True` additionally
# permits any non-terminal → any non-terminal (the "advance a missed task" feature).
# Terminal tasks are always immutable, even for operators.
OPERATOR_TRANSITIONS: dict[str, set[str]] = {
    "approved": {"submitted", "pending-approval"},
    "cancelled": NON_TERMINAL_STATUSES,  # any non-terminal task may be cancelled
}

QUARANTINE_DIRNAME = "quarantine"

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


def _load_all_tasks(
    queue_dir: str,
    include_archived: bool = False,
    include_quarantined: bool = False,
) -> list[dict]:
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

    subdirs = []
    if include_archived:
        subdirs.append("archive")
    if include_quarantined:
        subdirs.append(QUARANTINE_DIRNAME)

    for subdir in subdirs:
        for path in glob.glob(os.path.join(queue_dir, subdir, "*.yml")):
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
    return datetime.now(timezone.utc)


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
        "alert_state": {
            "first_alerted_at": None,
            "last_alerted_at": None,
            "alert_count": 0,
        },
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
        created = task.get("created")
        ttl_days = task.get("ttl_days", 30)
        if created and isinstance(created, datetime) and now > created + timedelta(days=ttl_days):
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
        return datetime.min.replace(tzinfo=timezone.utc)

    filtered.sort(key=_sort_key, reverse=True)

    return [{k: v for k, v in t.items() if k != "_path"} for t in filtered[:limit]]


def get_task_handler(task_id: str, queue_dir: str | None = None) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    # Search main queue, then archive and quarantine
    tasks = _load_all_tasks(queue_dir, include_archived=True, include_quarantined=True)
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

        # alert_state and retry_policy are owned by the task-dispatcher — never modify them
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
      - any non-terminal → any non-terminal (only with allow_override=True; the
        deliberate "advance a missed task" override — a non-empty note is required)

    Terminal tasks (completed/failed/cancelled) are immutable. Archived and quarantined
    tasks cannot be mutated (restore first). Every change appends a history entry.
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

        if not (standard_ok or override_ok):
            return {
                "ok": False,
                "error": (
                    f"Invalid operator transition: {current_status!r} → {status!r}. "
                    f"Standard targets: approved (from submitted/pending-approval), "
                    f"cancelled (from any non-terminal). For other non-terminal moves pass "
                    f"allow_override=True."
                ),
            }

        if override_ok and not standard_ok and not (note and note.strip()):
            return {
                "ok": False,
                "error": "an override transition requires a non-empty note for the audit trail",
            }

        now = _now()
        task["status"] = status

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
        if override_ok and not standard_ok:
            history_entry["override"] = True
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        # alert_state and retry_policy are owned by the task-dispatcher — never modify them
        path = task.pop("_path")
        _write_task_atomic(path, task)

    logger.info(
        "task.operator_transition id=%s %s→%s actor=%s override=%s",
        task_id[:8],
        current_status,
        status,
        actor,
        override_ok and not standard_ok,
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


def _move_task_file(src: str, dest_dir: str) -> str:
    """Atomically move a task YAML into dest_dir (created if needed). Returns the new path."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    os.rename(src, dest)
    return dest


def quarantine_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    queue_dir: str | None = None,
) -> dict:
    """
    Isolate a task by moving its YAML to <queue_dir>/quarantine/ (recoverable, not deleted).
    Quarantined tasks drop out of list_tasks but remain resolvable via get_task and
    restorable via restore_task. The task's status is preserved; the action is audited
    in history. There is intentionally no hard-delete.
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
        tasks = _load_all_tasks(queue_dir, include_archived=True, include_quarantined=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return {"ok": False, "error": "not found"}

        path = task.get("_path", "")
        if os.sep + "archive" + os.sep in path or path.endswith(os.sep + "archive"):
            return {"ok": False, "error": "task is archived and cannot be quarantined"}
        if os.sep + QUARANTINE_DIRNAME + os.sep in path:
            return {"ok": False, "error": "task is already quarantined"}

        now = _now()
        history_entry = {
            "timestamp": now,
            "status": task.get("status"),
            "actor": actor,
            "note": note or "Quarantined by operator",
            "action": "quarantine",
        }
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        task.pop("_path")
        # Persist the history entry at the source path, then move atomically into quarantine.
        _write_task_atomic(path, task)
        dest = _move_task_file(path, os.path.join(queue_dir, QUARANTINE_DIRNAME))

    logger.info("task.quarantine id=%s actor=%s -> %s", task_id[:8], actor, dest)
    return {"ok": True, "task_id": task_id}


def restore_task_handler(
    task_id: str,
    actor: str,
    note: str = "",
    queue_dir: str | None = None,
) -> dict:
    """
    Restore a quarantined task: move its YAML back to the active queue dir and audit it.
    Reverses quarantine_task. Errors if the task is not currently quarantined.
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
        tasks = _load_all_tasks(queue_dir, include_quarantined=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return {"ok": False, "error": "not found"}

        path = task.get("_path", "")
        if os.sep + QUARANTINE_DIRNAME + os.sep not in path:
            return {"ok": False, "error": "task is not quarantined"}

        now = _now()
        history_entry = {
            "timestamp": now,
            "status": task.get("status"),
            "actor": actor,
            "note": note or "Restored from quarantine by operator",
            "action": "restore",
        }
        if task.get("history") is None:
            task["history"] = []
        task["history"].append(history_entry)

        task.pop("_path")
        _write_task_atomic(path, task)
        dest = _move_task_file(path, queue_dir)

    logger.info("task.restore id=%s actor=%s -> %s", task_id[:8], actor, dest)
    return {"ok": True, "task_id": task_id}
