import fcntl
import os
import glob
import uuid
import logging
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_STATUSES = {"submitted", "approved", "pending-approval", "in-progress", "completed", "failed"}
VALID_PRIORITIES = {"normal", "high", "urgent"}
VALID_TASK_TYPES = {"build", "deploy", "fix", "research", "review", "audit", "notify"}
TERMINAL_STATUSES = {"completed", "failed"}

# Valid source statuses for each target transition in update_task
VALID_TRANSITIONS: dict[str, set[str]] = {
    "in-progress": {"approved"},          # agents must not claim unapproved tasks
    "completed": {"in-progress"},
    "failed": VALID_STATUSES - TERMINAL_STATUSES,
}

# context_refs validation: enforce absolute paths (must start with '/').
# Trust model: we do not restrict to a specific prefix allowlist — consumers
# are responsible for validating that dereferenced paths are accessible and safe.
# This is the accepted trust model for internal agent-to-agent coordination.
_CONTEXT_REF_MIN_LEN = 2  # at minimum "/<char>"


def _load_task_file(path: str) -> Optional[dict]:
    """Load a single YAML task file. Returns None on parse or type error."""
    try:
        with open(path, "r") as f:
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
    return datetime.now(timezone.utc)


def _validate_context_refs(context_refs: list) -> Optional[str]:
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
    context_refs: list = None,
    ttl_days: int = 30,
    queue_dir: str = None,
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
        return {"ok": False, "error": f"Invalid task_type: {task_type!r}. Must be one of: {sorted(VALID_TASK_TYPES)}"}
    if risk_level not in VALID_RISK_LEVELS:
        return {"ok": False, "error": f"Invalid risk_level: {risk_level!r}. Must be one of: {sorted(VALID_RISK_LEVELS)}"}
    if priority not in VALID_PRIORITIES:
        return {"ok": False, "error": f"Invalid priority: {priority!r}. Must be one of: {sorted(VALID_PRIORITIES)}"}
    if not isinstance(ttl_days, int) or ttl_days < 1:
        return {"ok": False, "error": f"Invalid ttl_days: {ttl_days!r}. Must be a positive integer."}

    if context_refs:
        err = _validate_context_refs(context_refs)
        if err:
            return {"ok": False, "error": err}

    task_id = str(uuid.uuid4())
    now = _now()
    slug = task_id[:8]
    filename = f"{now.strftime('%Y%m%d-%H%M%S')}-{slug}.yml"
    path = os.path.join(queue_dir, filename)

    task = {
        "id": task_id,
        "created": now,
        "source_agent": source_agent,
        "target_agent": target_agent,
        "task_type": task_type,
        "risk_level": risk_level,
        "requires_approval": requires_approval,
        "status": "submitted",
        "summary": summary,
        "ttl_days": ttl_days,
        "payload": {
            "description": description,
            "context_refs": context_refs,
            "priority": priority,
        },
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
    target_agent: str = None,
    source_agent: str = None,
    status: str = None,
    task_type: str = None,
    include_archived: bool = False,
    limit: int = 20,
    queue_dir: str = None,
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
        if created and isinstance(created, datetime):
            if now > created + timedelta(days=ttl_days):
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


def get_task_handler(task_id: str, queue_dir: str = None) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    # Search main queue first, then archive
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
    output: str = None,
    queue_dir: str = None,
) -> dict:
    if queue_dir is None:
        queue_dir = os.environ.get("TASK_QUEUE_DIR", "/task-queue")

    try:
        uuid.UUID(task_id)
    except ValueError:
        return {"ok": False, "error": "invalid task_id format"}

    valid_update_statuses = {"in-progress", "completed", "failed"}
    if status not in valid_update_statuses:
        return {"ok": False, "error": f"Invalid status: {status!r}. update_task accepts: {sorted(valid_update_statuses)}"}

    with _task_lock(queue_dir, task_id):
        tasks = _load_all_tasks(queue_dir, include_archived=True)
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if task is None:
            return {"ok": False, "error": "not found"}

        if "archive" in task.get("_path", ""):
            return {"ok": False, "error": "task is archived and cannot be updated"}

        current_status = task.get("status")

        if current_status in TERMINAL_STATUSES:
            return {"ok": False, "error": f"Task is in terminal status {current_status!r} and cannot be updated"}

        allowed_from = VALID_TRANSITIONS.get(status, set())
        if current_status not in allowed_from:
            return {
                "ok": False,
                "error": f"Invalid transition: {current_status!r} → {status!r}. Allowed from: {sorted(allowed_from)}",
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

    return {"ok": True, "task_id": task_id}
