import re
import uuid
from datetime import datetime, timedelta, timezone

import yaml

from src.tools.queue import (
    cancel_task_handler,
    get_task_handler,
    list_tasks_handler,
    quarantine_task_handler,
    restore_task_handler,
    set_task_status_handler,
    submit_task_handler,
    update_task_handler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(tmp_path, **kwargs) -> dict:
    """Submit a task with sensible defaults. Returns the submit result dict."""
    defaults = dict(
        source_agent="test-agent",
        target_agent="dev",
        task_type="build",
        summary="Test task",
        description="Test description",
        queue_dir=str(tmp_path),
    )
    defaults.update(kwargs)
    return submit_task_handler(**defaults)


def set_task_status(tmp_path, result: dict, status: str) -> None:
    """Directly patch a task's status on disk (simulates dispatcher transitions)."""
    path = str(tmp_path / result["filename"])
    with open(path) as f:
        data = yaml.safe_load(f)
    data["status"] = status
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# submit_task tests
# ---------------------------------------------------------------------------


def test_submit_creates_file(tmp_path):
    result = make_task(tmp_path)

    assert result["ok"] is True
    assert "task_id" in result
    assert "filename" in result

    task_file = tmp_path / result["filename"]
    assert task_file.exists()

    with open(task_file) as f:
        data = yaml.safe_load(f)

    assert data["id"] == result["task_id"]
    assert data["status"] == "submitted"
    assert data["source_agent"] == "test-agent"
    assert data["target_agent"] == "dev"
    assert len(data["history"]) == 1
    assert data["history"][0]["status"] == "submitted"
    assert data["alert_state"]["alert_count"] == 0
    assert data["retry_policy"]["retry_count"] == 0


def test_submit_adversarial_strings(tmp_path):
    """yaml.dump must correctly escape adversarial strings — no string interpolation."""
    adversarial_summary = "evil: injected\n  nested: true\ncolons: {key: val}"
    result = submit_task_handler(
        source_agent="agent: with colon",
        target_agent="dev",
        task_type="build",
        summary=adversarial_summary,
        description="line1\n  indented: yes\nline2",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is True

    with open(tmp_path / result["filename"]) as f:
        data = yaml.safe_load(f)

    # Strings must roundtrip exactly after yaml.dump escaping
    assert data["summary"] == adversarial_summary
    assert data["source_agent"] == "agent: with colon"


def test_submit_atomic_filename(tmp_path):
    result = make_task(tmp_path)
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{8}\.yml$", result["filename"]), (
        f"Filename does not match expected pattern: {result['filename']}"
    )


def test_submit_invalid_risk_level(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        risk_level="extreme",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "risk_level" in result["error"]


def test_submit_invalid_priority(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        priority="critical",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "priority" in result["error"]


def test_submit_invalid_context_ref_relative(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        context_refs=["relative/path"],
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "context_ref" in result["error"]


def test_submit_valid_context_ref(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        context_refs=["/home/ted/.claude/comms/artifacts/build-plans/foo/plan.md"],
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is True


def test_submit_invalid_workflow_mode(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        workflow_mode="manual",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "workflow_mode" in result["error"]


def test_submit_workflow_mode_default_semi_auto(tmp_path):
    result = make_task(tmp_path)
    assert result["ok"] is True

    with open(tmp_path / result["filename"]) as f:
        data = yaml.safe_load(f)

    assert data["workflow_mode"] == "semi-auto"


def test_submit_workflow_mode_auto(tmp_path):
    result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        workflow_mode="auto",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is True

    with open(tmp_path / result["filename"]) as f:
        data = yaml.safe_load(f)

    assert data["workflow_mode"] == "auto"


def test_get_task_returns_workflow_mode(tmp_path):
    submit_result = submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        workflow_mode="auto",
        queue_dir=str(tmp_path),
    )
    task = get_task_handler(task_id=submit_result["task_id"], queue_dir=str(tmp_path))
    assert task["workflow_mode"] == "auto"


def test_list_tasks_returns_workflow_mode(tmp_path):
    submit_task_handler(
        source_agent="a",
        target_agent="b",
        task_type="build",
        summary="s",
        description="d",
        workflow_mode="auto",
        queue_dir=str(tmp_path),
    )
    results = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(results) == 1
    assert results[0]["workflow_mode"] == "auto"


# ---------------------------------------------------------------------------
# list_tasks tests
# ---------------------------------------------------------------------------


def test_list_filters_by_target(tmp_path):
    make_task(tmp_path, target_agent="dev")
    make_task(tmp_path, target_agent="research")

    results = list_tasks_handler(target_agent="dev", queue_dir=str(tmp_path))
    assert len(results) == 1
    assert results[0]["target_agent"] == "dev"


def test_list_filters_by_status(tmp_path):
    make_task(tmp_path)

    results = list_tasks_handler(status="submitted", queue_dir=str(tmp_path))
    assert len(results) == 1

    results = list_tasks_handler(status="approved", queue_dir=str(tmp_path))
    assert len(results) == 0


def test_list_multiple_status_filter(tmp_path):
    make_task(tmp_path)

    results = list_tasks_handler(status="submitted,approved", queue_dir=str(tmp_path))
    assert len(results) == 1


def test_list_skips_tmp_files(tmp_path):
    # Place a .tmp file — should be ignored
    (tmp_path / "fake.tmp").write_text("id: fake\n")

    results = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(results) == 0


def test_list_include_archived(tmp_path):
    make_task(tmp_path)

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = {
        "id": str(uuid.uuid4()),
        "created": datetime.now(timezone.utc),
        "source_agent": "test",
        "target_agent": "dev",
        "task_type": "build",
        "status": "completed",
        "summary": "Archived task",
        "ttl_days": 30,
    }
    with open(archive_dir / "archived.yml", "w") as f:
        yaml.dump(archived, f)

    # Default: archive excluded
    results = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(results) == 1

    # include_archived=True: both returned
    results = list_tasks_handler(include_archived=True, queue_dir=str(tmp_path))
    assert len(results) == 2


def test_list_excludes_expired_tasks(tmp_path):
    result = make_task(tmp_path, ttl_days=1)

    # Patch created to 2 days ago so the task is expired
    path = str(tmp_path / result["filename"])
    with open(path) as f:
        data = yaml.safe_load(f)
    data["created"] = datetime.now(timezone.utc) - timedelta(days=2)
    with open(path, "w") as f:
        yaml.dump(data, f)

    results = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(results) == 0


# ---------------------------------------------------------------------------
# get_task tests
# ---------------------------------------------------------------------------


def test_get_task_found(tmp_path):
    result = make_task(tmp_path)
    task_id = result["task_id"]

    found = get_task_handler(task_id=task_id, queue_dir=str(tmp_path))
    assert found["id"] == task_id
    assert found["status"] == "submitted"
    assert "_path" not in found


def test_get_task_not_found(tmp_path):
    found = get_task_handler(task_id=str(uuid.uuid4()), queue_dir=str(tmp_path))
    assert found["ok"] is False
    assert found["error"] == "not found"


def test_get_task_invalid_id(tmp_path):
    found = get_task_handler(task_id="not-a-uuid", queue_dir=str(tmp_path))
    assert found["ok"] is False
    assert "invalid" in found["error"]


# ---------------------------------------------------------------------------
# update_task tests
# ---------------------------------------------------------------------------


def test_update_claim_from_approved(tmp_path):
    """approved → in-progress should succeed and append a history entry."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="dev",
        note="Claiming task",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is True

    updated = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert updated["status"] == "in-progress"
    assert updated["history"][-1]["actor"] == "dev"
    assert updated["history"][-1]["note"] == "Claiming task"


def test_update_claim_from_submitted_rejected(tmp_path):
    """submitted → in-progress must fail — task not yet approved."""
    result = make_task(tmp_path)

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is False
    assert "approved" in update_result["error"]


def test_update_completed(tmp_path):
    """in-progress → completed must set result fields."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="completed",
        actor="dev",
        output="Build successful.",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is True

    updated = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert updated["status"] == "completed"
    assert updated["result"]["output"] == "Build successful."
    assert updated["result"]["completed_by"] == "dev"
    assert updated["result"]["completed_at"] is not None


def test_update_illegal_backwards(tmp_path):
    """completed → in-progress must fail; file must remain unchanged."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "completed")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is False

    unchanged = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert unchanged["status"] == "completed"


def test_update_preserves_alert_state(tmp_path):
    """update_task must never touch alert_state or retry_policy."""
    result = make_task(tmp_path)
    path = str(tmp_path / result["filename"])

    with open(path) as f:
        data = yaml.safe_load(f)
    data["status"] = "approved"
    data["alert_state"] = {
        "first_alerted_at": "2026-01-01",
        "last_alerted_at": "2026-01-02",
        "alert_count": 3,
    }
    data["retry_policy"] = {
        "next_retry_at": "2026-01-03",
        "retry_count": 2,
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="dev",
        queue_dir=str(tmp_path),
    )

    updated = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert updated["alert_state"]["alert_count"] == 3
    assert updated["retry_policy"]["retry_count"] == 2


# ---------------------------------------------------------------------------
# Phase 2 additive tests
# ---------------------------------------------------------------------------


def test_submit_empty_source_agent_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="",
        target_agent="dev",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "source_agent" in result["error"]


def test_submit_blank_target_agent_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="   ",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "target_agent" in result["error"]


def test_submit_invalid_task_type_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="dev",
        task_type="unknown_type",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "task_type" in result["error"]


def test_submit_all_valid_task_types_accepted(tmp_path):
    from src.tools.queue import VALID_TASK_TYPES

    for t in VALID_TASK_TYPES:
        result = submit_task_handler(
            source_agent="dev",
            target_agent="dev",
            task_type=t,
            summary="s",
            description="d",
            queue_dir=str(tmp_path),
        )
        assert result["ok"] is True, f"task_type={t!r} should be valid"


def test_update_archived_task_returns_error(tmp_path):
    result = make_task(tmp_path)
    task_id = result["task_id"]

    # Move task file to archive
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    src = tmp_path / result["filename"]
    dst = archive_dir / result["filename"]
    src.rename(dst)

    update_result = update_task_handler(
        task_id=task_id,
        status="in-progress",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is False
    assert "archived" in update_result["error"]


def test_update_output_none_preserved(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")

    update_task_handler(
        task_id=result["task_id"],
        status="completed",
        actor="dev",
        output=None,
        queue_dir=str(tmp_path),
    )

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["result"]["output"] is None


def test_ttl_boundary_zero_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="dev",
        task_type="build",
        summary="s",
        description="d",
        ttl_days=0,
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False


def test_ttl_boundary_negative_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="dev",
        task_type="build",
        summary="s",
        description="d",
        ttl_days=-1,
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False


def test_ttl_boundary_one_accepted(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="dev",
        task_type="build",
        summary="s",
        description="d",
        ttl_days=1,
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# originating_task_id tests
# ---------------------------------------------------------------------------


def test_submit_originating_task_id_stored(tmp_path):
    parent_id = str(uuid.uuid4())
    result = submit_task_handler(
        source_agent="security",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
        originating_task_id=parent_id,
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is True

    with open(tmp_path / result["filename"]) as f:
        data = yaml.safe_load(f)

    assert data["payload"]["originating_task_id"] == parent_id


def test_submit_originating_task_id_none_omitted(tmp_path):
    result = make_task(tmp_path)
    assert result["ok"] is True

    with open(tmp_path / result["filename"]) as f:
        data = yaml.safe_load(f)

    assert "originating_task_id" not in data["payload"]


def test_submit_originating_task_id_invalid_uuid_rejected(tmp_path):
    result = submit_task_handler(
        source_agent="dev",
        target_agent="dev",
        task_type="build",
        summary="s",
        description="d",
        originating_task_id="not-a-uuid",
        queue_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert "originating_task_id" in result["error"]


# ---------------------------------------------------------------------------
# set_task_status (operator transitions) tests
# ---------------------------------------------------------------------------


def test_set_status_approve_from_submitted(tmp_path):
    result = make_task(tmp_path)  # starts 'submitted'
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="approved",
        actor="ted",
        note="approving",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "approved"
    assert task["history"][-1]["actor"] == "ted"


def test_set_status_approve_from_pending_approval(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "pending-approval")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="approved",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True


def test_set_status_approve_from_in_progress_rejected(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="approved",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid operator transition" in r["error"]


def test_set_status_cancel_from_approved(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="cancelled",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "cancelled"
    assert task["result"]["completed_by"] == "ted"
    assert task["result"]["completed_at"] is not None


def test_set_status_invalid_status_rejected(tmp_path):
    result = make_task(tmp_path)
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="bogus",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid status" in r["error"]


def test_set_status_invalid_uuid_rejected(tmp_path):
    r = set_task_status_handler(
        task_id="not-a-uuid",
        status="approved",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "invalid task_id" in r["error"]


def test_set_status_empty_actor_rejected(tmp_path):
    result = make_task(tmp_path)
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="approved",
        actor="  ",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "actor" in r["error"]


def test_set_status_not_found(tmp_path):
    r = set_task_status_handler(
        task_id=str(uuid.uuid4()),
        status="approved",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert r["error"] == "not found"


def test_set_status_terminal_is_immutable(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "completed")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="approved",
        actor="ted",
        allow_override=True,
        note="try",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "terminal" in r["error"]


def test_set_status_archived_rejected(tmp_path):
    result = make_task(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (tmp_path / result["filename"]).rename(archive_dir / result["filename"])
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="cancelled",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "archived" in r["error"]


def test_set_status_override_advances_missed_task(tmp_path):
    """allow_override moves a stuck approved task forward to in-progress, audited."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="ted",
        note="agent missed it",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "in-progress"
    assert task["history"][-1]["override"] is True
    assert task["history"][-1]["note"] == "agent missed it"


def test_set_status_override_requires_note(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="ted",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "note" in r["error"]


def test_set_status_override_cannot_reach_terminal(tmp_path):
    """Override is non-terminal → non-terminal only; it cannot set completed/failed."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="completed",
        actor="ted",
        note="nope",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid operator transition" in r["error"]


# ---------------------------------------------------------------------------
# cancel_task tests
# ---------------------------------------------------------------------------


def test_cancel_task_from_approved(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = cancel_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "cancelled"
    # Default note applied
    assert task["history"][-1]["note"] == "Cancelled by operator"

    # Cancel is terminal-by-status, not a move: the record stays on disk (recoverable
    # as a record, never hard-deleted). Clients filter terminal statuses out of the
    # "active" view; the MCP still surfaces it like any completed/failed task.
    assert (tmp_path / result["filename"]).exists()
    listed = list_tasks_handler(status="cancelled", queue_dir=str(tmp_path))
    assert len(listed) == 1 and listed[0]["id"] == result["task_id"]


def test_cancel_task_terminal_rejected(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "completed")
    r = cancel_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "terminal" in r["error"]


def test_cancel_task_custom_note(tmp_path):
    result = make_task(tmp_path)
    r = cancel_task_handler(
        task_id=result["task_id"],
        actor="ted",
        note="stale",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["history"][-1]["note"] == "stale"


# ---------------------------------------------------------------------------
# quarantine / restore tests
# ---------------------------------------------------------------------------


def test_quarantine_moves_file_and_hides_from_list(tmp_path):
    result = make_task(tmp_path)
    r = quarantine_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True

    # File moved into quarantine/
    assert not (tmp_path / result["filename"]).exists()
    assert (tmp_path / "quarantine" / result["filename"]).exists()

    # Hidden from list_tasks (even with include_archived)
    assert len(list_tasks_handler(queue_dir=str(tmp_path))) == 0
    assert len(list_tasks_handler(include_archived=True, queue_dir=str(tmp_path))) == 0

    # Still resolvable via get_task, with an audited history entry
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["id"] == result["task_id"]
    assert task["history"][-1]["action"] == "quarantine"


def test_quarantine_already_quarantined_rejected(tmp_path):
    result = make_task(tmp_path)
    quarantine_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    r = quarantine_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "already quarantined" in r["error"]


def test_quarantine_archived_rejected(tmp_path):
    result = make_task(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (tmp_path / result["filename"]).rename(archive_dir / result["filename"])
    r = quarantine_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "archived" in r["error"]


def test_quarantine_not_found(tmp_path):
    r = quarantine_task_handler(task_id=str(uuid.uuid4()), actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert r["error"] == "not found"


def test_quarantine_invalid_uuid(tmp_path):
    r = quarantine_task_handler(task_id="nope", actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "invalid task_id" in r["error"]


def test_quarantine_empty_actor_rejected(tmp_path):
    result = make_task(tmp_path)
    r = quarantine_task_handler(task_id=result["task_id"], actor="", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "actor" in r["error"]


def test_restore_round_trip(tmp_path):
    result = make_task(tmp_path)
    quarantine_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))

    r = restore_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True

    # Back in the active queue dir and visible in list_tasks again
    assert (tmp_path / result["filename"]).exists()
    assert not (tmp_path / "quarantine" / result["filename"]).exists()
    listed = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(listed) == 1
    assert listed[0]["id"] == result["task_id"]

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["history"][-1]["action"] == "restore"


def test_restore_not_quarantined_rejected(tmp_path):
    result = make_task(tmp_path)
    r = restore_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "not quarantined" in r["error"]


def test_restore_not_found(tmp_path):
    r = restore_task_handler(task_id=str(uuid.uuid4()), actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert r["error"] == "not found"


def test_restore_invalid_uuid(tmp_path):
    r = restore_task_handler(task_id="nope", actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "invalid task_id" in r["error"]
