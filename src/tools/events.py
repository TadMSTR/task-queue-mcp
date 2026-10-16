"""
Owner-side task lifecycle events, written to agent-bus's producer outbox.

This server is the component that writes every task transition, so it is the one that
says each transition happened. Until v0.14.0 agents logged `task.dispatched` /
`task.completed` / `task.failed` by hand, from their CLAUDE.md, and most transitions were
never logged at all: over 30 days the bus held 123 `task.completed` against 448
completions in the queue, writer and security emitted none, and `submitted`, `parked`
and `cancelled` had no event of any kind.

TRANSPORT. The container is read-only and its only mount is the queue directory, so the
outbox lives inside it, at `<queue>/.outbox/`. agent-bus drains it into its chained log.
Every queue reader globs top-level `*.yml` only, so a dot-directory is invisible to them,
as `.locks/` already is. Mounting the bus log in here was rejected: it would give a
network-reachable container write access to every agent's hash-chained log.

THE FILE CONTRACT is agent-bus's (README "Producer Outboxes"); this is one of three
independent ~30-line writers of it. tests/test_events.py runs this writer's output through
agent-bus's own validator, which is what makes three writers acceptable.

TWO RULES THE CALL SITES KEEP:

- Emit AFTER `_write_task_atomic` succeeds, still inside `_task_lock`. A crash between
  the two loses an event; it never produces an event for a write that did not happen.
  The coverage check (operator-panel part 3, Phase 7) is what finds a loss.
- Event loss must never block a queue write. Every failure here is logged at error and
  swallowed. `emit()` never raises.

NEVER `payload.description`. The summary is the task summary cut to 200 characters, and
the metadata is a fixed set of short fields. A description can carry anything an agent
wrote, and bus events are display-only text read by other agents and, later, a panel.

Stdlib only, like the rest of the domain layer.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

OUTBOX_DIRNAME = ".outbox"
EVENT_SOURCE = "task-queue-mcp"
SCHEMA_VERSION = 1
SUMMARY_CHARS = 200
# Per-value cap on the free-ish strings (actor, channel, agent names). They are short in
# practice; this keeps one odd value from pushing an event over the 4 KiB metadata limit,
# where agent-bus would quarantine it and the transition would go unrecorded.
FIELD_CHARS = 128

# to_status -> event, for an ordinary transition. Two cases are decided before this table:
# creation (no from_status) and leaving `parked`. See event_for().
_BY_TARGET = {
    # Only reachable by an operator override or a requeue. Either way the task has gone
    # back to the start of the queue, which is what `requeued` says.
    "submitted": "task.requeued",
    "approved": "task.approved",
    "pending-approval": "task.pending-approval",
    "in-progress": "task.started",
    "parked": "task.parked",
    "routing-failed": "task.routing-failed",
    "completed": "task.completed",
    "failed": "task.failed",
    "cancelled": "task.cancelled",
}

ACTION_AMEND = "amend"
ACTION_REQUEUE = "requeue"
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def event_for(from_status: str | None, to_status: str, action: str | None = None) -> str:
    """The event type for one transition. Raises KeyError for an unmapped target.

    Raising is deliberate: tests/test_events.py walks every (from, to) pair, and an
    unmapped transition must fail that test rather than silently emit nothing.
    """
    if action == ACTION_AMEND:
        return "task.amended"
    if action == ACTION_REQUEUE:
        return "task.requeued"
    if from_status is None:
        return "task.submitted"
    if from_status == "parked" and to_status != "parked" and to_status not in _TERMINAL:
        return "task.unparked"
    return _BY_TARGET[to_status]


def outbox_dir(queue_dir: str) -> str:
    return os.path.join(queue_dir, OUTBOX_DIRNAME)


def ensure_outbox(queue_dir: str) -> bool:
    """Create the outbox at startup, mode 0700. Logs at error and returns False if it can't.

    A missing outbox does not stop the server. Every emit will log its own failure, and
    agent-bus's get_status shows the root as absent.
    """
    path = outbox_dir(queue_dir)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        probe = os.path.join(path, f".probe-{uuid.uuid4()}")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError as exc:
        logger.error("event outbox %s is not writable — task events will be lost: %s", path, exc)
        return False


def _short(value: object) -> object:
    if isinstance(value, str) and len(value) > FIELD_CHARS:
        return value[:FIELD_CHARS]
    return value


def build_metadata(
    task: dict,
    from_status: str | None,
    to_status: str,
    actor: str | None,
    channel: str | None = None,
    on_behalf_of: str | None = None,
) -> dict:
    """The event's metadata: a fixed set of fields, None values omitted."""
    fields = {
        "task_id": task.get("id"),
        "from_status": from_status,
        "to_status": to_status,
        "actor": actor,
        "channel": channel,
        "on_behalf_of": on_behalf_of,
        "source_agent": task.get("source_agent"),
        "target_agent": task.get("target_agent"),
        "task_type": task.get("task_type"),
        "workflow_mode": task.get("workflow_mode"),
        "build_name": task.get("build_name"),
        "audit_round": task.get("audit_round"),
        "schema": SCHEMA_VERSION,
    }
    return {k: _short(v) for k, v in fields.items() if v is not None}


def emit(
    queue_dir: str,
    task: dict,
    from_status: str | None,
    to_status: str,
    actor: str | None,
    *,
    channel: str | None = None,
    on_behalf_of: str | None = None,
    action: str | None = None,
) -> str | None:
    """Write one event file for a transition that has already been written. Never raises.

    Returns the event file's path, or None if nothing was written.
    """
    try:
        event = event_for(from_status, to_status, action)
        now = datetime.now(UTC)
        eid = str(uuid.uuid4())
        summary = task.get("summary")
        body = {
            "id": eid,
            "ts": now.isoformat(),
            "event": event,
            "source": EVENT_SOURCE,
            "target": _short(task.get("target_agent")),
            "summary": (summary if isinstance(summary, str) else "")[:SUMMARY_CHARS],
            "metadata": build_metadata(task, from_status, to_status, actor, channel, on_behalf_of),
        }
        directory = outbox_dir(queue_dir)
        # Normally made at startup. Re-made here so a deleted outbox costs nothing.
        os.makedirs(directory, mode=0o700, exist_ok=True)
        final = os.path.join(directory, f"{now.strftime('%Y%m%dT%H%M%S.%f')}-{eid}.json")
        tmp = os.path.join(directory, f".tmp-{eid}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(body, f, ensure_ascii=False, default=str)
        os.rename(tmp, final)
        return final
    except Exception:
        logger.error(
            "task event not written: %s→%s for %s",
            from_status,
            to_status,
            str(task.get("id"))[:8],
            exc_info=True,
        )
        return None
