import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from src.tools.queue import (
    AUTO_CLOSE_FROM_STATUSES,
    MAX_AMENDMENT_CHARS,
    MAX_AMENDMENTS,
    NON_TERMINAL_STATUSES,
    OPERATOR_ACTOR,
    TERMINAL_STATUSES,
    VALID_STATUSES,
    VALID_TRANSITIONS,
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
    assert data["retry_policy"]["retry_count"] == 0
    # alert_state was retired in 0.4.0 — the emitter is gone, so we no longer write a
    # block nothing reads. Existing YAMLs keep theirs; it is inert.
    assert "alert_state" not in data


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


def test_list_rejects_unrecognised_status(tmp_path):
    """
    The exact bug this guards: writer/CLAUDE.md swept with status="pending", which is not a
    status this server has, and got [] back for months — indistinguishable from an empty
    queue. An empty list is a legitimate answer to a well-formed question, so the typo has
    to be refused rather than filtered on.
    """
    make_task(tmp_path)

    with pytest.raises(ValueError, match="Invalid status filter"):
        list_tasks_handler(status="pending", queue_dir=str(tmp_path))


def test_list_rejects_unrecognised_status_among_valid_ones(tmp_path):
    make_task(tmp_path)

    with pytest.raises(ValueError, match="pending"):
        list_tasks_handler(status="approved,pending", queue_dir=str(tmp_path))


def test_list_rejects_status_of_only_separators(tmp_path):
    make_task(tmp_path)

    with pytest.raises(ValueError, match="Invalid status filter"):
        list_tasks_handler(status=",", queue_dir=str(tmp_path))


def test_list_tolerates_whitespace_and_trailing_comma(tmp_path):
    make_task(tmp_path)

    results = list_tasks_handler(status=" submitted , approved ,", queue_dir=str(tmp_path))
    assert len(results) == 1


def test_list_empty_status_is_no_filter(tmp_path):
    """An empty string is falsy and has always meant "no filter" — not a rejected value."""
    make_task(tmp_path)

    assert len(list_tasks_handler(status="", queue_dir=str(tmp_path))) == 1


def test_list_accepts_every_valid_status(tmp_path):
    """Guards against a status entering VALID_STATUSES but not being filterable."""
    for status in VALID_STATUSES:
        assert list_tasks_handler(status=status, queue_dir=str(tmp_path)) == []


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
        "created": datetime.now(UTC),
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
    data["created"] = datetime.now(UTC) - timedelta(days=2)
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


def test_update_preserves_legacy_alert_state(tmp_path):
    """
    update_task must never touch retry_policy, nor the residual alert_state block on
    pre-0.4.0 task files. We stopped writing alert_state on create, but the 376 YAMLs that
    already carry one are left as-is — a mutation must not silently strip them.
    """
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


def test_submit_accepts_task_types_the_agent_docs_already_use(tmp_path):
    """
    These three were documented in agent CLAUDE.md files before they existed here, so every
    call using them failed validation. Named explicitly rather than left to the generic
    loop above, so removing one is a test failure and not a silent regression.
    """
    from src.tools.queue import VALID_TASK_TYPES

    assert {"docs", "ticket_audit", "ticket_audit_complete"} <= VALID_TASK_TYPES


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


def test_update_non_target_agent_rejected(tmp_path):
    """A non-target agent must not be able to transition a task, even legally."""
    result = make_task(tmp_path, target_agent="dev")
    set_task_status(tmp_path, result, "approved")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="research",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is False
    assert "not the target agent" in update_result["error"]

    unchanged = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert unchanged["status"] == "approved"


def test_update_target_agent_still_succeeds(tmp_path):
    """The task's actual target agent must still be able to update it."""
    result = make_task(tmp_path, target_agent="dev")
    set_task_status(tmp_path, result, "approved")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is True


def test_update_operator_still_succeeds(tmp_path):
    """The operator actor must bypass the target_agent check like everywhere else."""
    result = make_task(tmp_path, target_agent="dev")
    set_task_status(tmp_path, result, "approved")

    update_result = update_task_handler(
        task_id=result["task_id"],
        status="in-progress",
        actor=OPERATOR_ACTOR,
        queue_dir=str(tmp_path),
    )
    assert update_result["ok"] is True


def test_valid_transitions_failed_is_a_literal_set():
    """
    Pin VALID_TRANSITIONS["failed"] as an explicit literal, not NON_TERMINAL_STATUSES.
    A future status addition (like `parked` did for the park work) must not silently
    widen this set again — see vikunja#325.
    """
    assert VALID_TRANSITIONS["failed"] == {
        "submitted",
        "pending-approval",
        "approved",
        "in-progress",
    }
    assert "routing-failed" not in VALID_TRANSITIONS["failed"]
    assert "parked" not in VALID_TRANSITIONS["failed"]


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
# auto-close on return-task submission
#
# The scenario throughout: `developer` submits an audit request targeting `security`;
# security files the audit and submits a return task naming the request as its parent.
# Submitting the return task is what closes the request.
# ---------------------------------------------------------------------------


def _request_task(tmp_path, target="security", source="developer", status="approved") -> dict:
    """Create a request task in `status` and return its submit result."""
    r = make_task(tmp_path, source_agent=source, target_agent=target, task_type="audit")
    if status != "submitted":
        set_task_status(tmp_path, r, status)
    return r


def _return_task(tmp_path, parent_id, source="security", target="developer") -> dict:
    return make_task(
        tmp_path,
        source_agent=source,
        target_agent=target,
        task_type="build",
        originating_task_id=parent_id,
    )


def test_auto_close_fires_on_return_task(tmp_path):
    parent = _request_task(tmp_path)

    child = _return_task(tmp_path, parent["task_id"])
    assert child["ok"] is True
    assert child["auto_closed_task_id"] == parent["task_id"]

    closed = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    assert closed["status"] == "completed"
    assert closed["result"]["completed_by"] == "security"
    assert child["task_id"] in closed["history"][-1]["note"]


def test_auto_close_walks_through_in_progress(tmp_path):
    """approved → completed is not a legal transition; the history must show the claim."""
    parent = _request_task(tmp_path)
    _return_task(tmp_path, parent["task_id"])

    closed = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    statuses = [h["status"] for h in closed["history"]]
    assert statuses[-2:] == ["in-progress", "completed"]
    assert all(h["actor"] == "security" for h in closed["history"][-2:])


def test_auto_close_fires_from_in_progress(tmp_path):
    """The agent claimed the task itself before working — no extra claim needed."""
    parent = _request_task(tmp_path, status="in-progress")
    child = _return_task(tmp_path, parent["task_id"])

    assert child["auto_closed_task_id"] == parent["task_id"]
    closed = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    assert closed["status"] == "completed"
    assert [h["status"] for h in closed["history"]][-1] == "completed"


def test_auto_close_does_not_fire_when_parent_targets_someone_else(tmp_path):
    """
    THE bound on this feature. Without it, any agent could close any open task by naming it
    as the parent of a task it submits.
    """
    parent = _request_task(tmp_path, target="sysadmin")

    child = _return_task(tmp_path, parent["task_id"], source="security")
    assert child["ok"] is True
    assert "auto_closed_task_id" not in child

    untouched = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    assert untouched["status"] == "approved"


def test_auto_close_does_not_fire_for_operator_source(tmp_path):
    """
    update_task_handler's ownership check also admits OPERATOR_ACTOR, so relying on it alone
    would let a caller submitting as source_agent="operator" close anyone's task. The
    explicit target_agent == source_agent check is what prevents that.
    """
    parent = _request_task(tmp_path, target="security")

    child = _return_task(tmp_path, parent["task_id"], source=OPERATOR_ACTOR)
    assert "auto_closed_task_id" not in child
    assert get_task_handler(parent["task_id"], queue_dir=str(tmp_path))["status"] == "approved"


def test_auto_close_does_not_fire_on_terminal_parent(tmp_path):
    parent = _request_task(tmp_path, status="in-progress")
    update_task_handler(
        task_id=parent["task_id"], status="completed", actor="security", queue_dir=str(tmp_path)
    )
    before = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))

    child = _return_task(tmp_path, parent["task_id"])
    assert child["ok"] is True
    assert "auto_closed_task_id" not in child

    after = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    assert after["history"] == before["history"]


def test_auto_close_does_not_fire_on_parked_parent(tmp_path):
    """Parking is a deliberate pause; auto-closing it would defeat the status entirely."""
    parent = _request_task(tmp_path)
    park_task_handler(task_id=parent["task_id"], actor="ted", queue_dir=str(tmp_path))

    child = _return_task(tmp_path, parent["task_id"])
    assert "auto_closed_task_id" not in child
    assert get_task_handler(parent["task_id"], queue_dir=str(tmp_path))["status"] == "parked"


def test_auto_close_does_not_fire_on_unapproved_parent(tmp_path):
    parent = _request_task(tmp_path, status="submitted")

    child = _return_task(tmp_path, parent["task_id"])
    assert "auto_closed_task_id" not in child
    assert get_task_handler(parent["task_id"], queue_dir=str(tmp_path))["status"] == "submitted"


def test_auto_close_does_not_fire_on_routing_failed_parent(tmp_path):
    """The dispatcher is still retrying it — closing would end a task mid-retry."""
    parent = _request_task(tmp_path, status="routing-failed")

    child = _return_task(tmp_path, parent["task_id"])
    assert "auto_closed_task_id" not in child
    after = get_task_handler(parent["task_id"], queue_dir=str(tmp_path))
    assert after["status"] == "routing-failed"


def test_auto_close_from_statuses_is_a_literal_not_derived(tmp_path):
    """
    Same lesson as VALID_TRANSITIONS: a set defined in terms of NON_TERMINAL_STATUSES would
    silently admit any future status addition. This one is a literal, and must stay one.
    """
    assert AUTO_CLOSE_FROM_STATUSES == {"approved", "in-progress"}
    assert AUTO_CLOSE_FROM_STATUSES < NON_TERMINAL_STATUSES


def test_auto_close_does_not_fire_on_archived_parent(tmp_path):
    parent = _request_task(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (tmp_path / parent["filename"]).rename(archive / parent["filename"])

    child = _return_task(tmp_path, parent["task_id"])
    assert child["ok"] is True
    assert "auto_closed_task_id" not in child
    assert get_task_handler(parent["task_id"], queue_dir=str(tmp_path))["status"] == "approved"


def test_auto_close_skipped_when_parent_unresolvable(tmp_path):
    child = _return_task(tmp_path, str(uuid.uuid4()))
    assert child["ok"] is True
    assert "auto_closed_task_id" not in child


def test_auto_close_absent_when_no_originating_task_id(tmp_path):
    assert "auto_closed_task_id" not in make_task(tmp_path)


def test_submit_succeeds_when_auto_close_raises(tmp_path, monkeypatch):
    """The fail-safe must never fail the submit it is a side effect of."""
    import src.tools.queue as q

    parent = _request_task(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(q, "update_task_handler", _boom)

    child = _return_task(tmp_path, parent["task_id"])
    assert child["ok"] is True
    assert "auto_closed_task_id" not in child
    assert (tmp_path / child["filename"]).exists()
    assert get_task_handler(parent["task_id"], queue_dir=str(tmp_path))["status"] == "approved"


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


def test_set_status_completed_hints_at_update_task(tmp_path):
    """in-progress→completed is rejected here, but the error must point the caller
    at update_task — the exact transition that tripped up the security agent."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="completed",
        actor="ted",
        note="done",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid operator transition" in r["error"]
    assert "update_task" in r["error"]


def test_set_status_cancel_does_not_hint_at_update_task(tmp_path):
    """A genuinely operator-only transition (→cancelled) must NOT mention update_task —
    the hint is only for transitions update_task actually accepts."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")
    # cancel from in-progress is a standard operator move → succeeds, no error to check.
    # Force the rejection path with an invalid target that update_task also rejects:
    # submitted is neither an operator target from in-progress nor accepted by update_task.
    r = set_task_status_handler(
        task_id=result["task_id"],
        status="submitted",
        actor="ted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid operator transition" in r["error"]
    assert "update_task" not in r["error"]


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
# park / unpark tests
# ---------------------------------------------------------------------------


def test_park_keeps_file_in_place_and_visible(tmp_path):
    """The whole point of park-as-status: the task stays exactly where it was."""
    result = make_task(tmp_path)
    r = park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True

    # File never moves — no subdirectory involved.
    assert (tmp_path / result["filename"]).exists()

    # Still listed. This is what quarantine got wrong.
    listed = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(listed) == 1
    assert listed[0]["status"] == "parked"

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["parked_from"] == "submitted"
    assert task["history"][-1]["status"] == "parked"


def test_park_from_each_non_terminal_status(tmp_path):
    for status in ("submitted", "pending-approval", "approved", "in-progress"):
        result = make_task(tmp_path)
        set_task_status(tmp_path, result, status)
        r = park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
        assert r["ok"] is True, f"park from {status} should be permitted"
        task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
        assert task["status"] == "parked"
        assert task["parked_from"] == status


def test_park_from_terminal_rejected(tmp_path):
    for status in ("completed", "failed", "cancelled"):
        result = make_task(tmp_path)
        set_task_status(tmp_path, result, status)
        r = park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
        assert r["ok"] is False, f"park from {status} must be rejected"
        assert "terminal" in r["error"]


def test_park_is_not_reachable_via_update_task(tmp_path):
    """Parking is an operator action, like cancel. Agents must not reach it."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    r = update_task_handler(
        task_id=result["task_id"],
        status="parked",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "update_task accepts" in r["error"]


def test_parked_task_survives_its_ttl(tmp_path):
    """
    A parked task past its TTL must still be listed. Parking is a deliberate bookmark; if
    it silently expired, the status would be actively worse than leaving the task alone.
    """
    result = make_task(tmp_path, ttl_days=1)
    path = tmp_path / result["filename"]
    with open(path) as f:
        data = yaml.safe_load(f)
    data["created"] = datetime.now(UTC) - timedelta(days=30)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # Unparked, it expires out of the listing.
    assert len(list_tasks_handler(queue_dir=str(tmp_path))) == 0

    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))

    listed = list_tasks_handler(queue_dir=str(tmp_path))
    assert len(listed) == 1
    assert listed[0]["status"] == "parked"


def test_unpark_restores_prior_status(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))

    r = unpark_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "approved"
    # The marker is cleared so it can never go stale.
    assert "parked_from" not in task
    assert task["history"][-1]["override"] is True


def test_unpark_to_explicit_status(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "approved")
    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))

    r = unpark_task_handler(
        task_id=result["task_id"],
        actor="ted",
        status="submitted",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "submitted"


def test_unpark_records_a_note(tmp_path):
    """Unpark is an override transition, which the handler requires a note for."""
    result = make_task(tmp_path)
    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    unpark_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["history"][-1]["note"].strip()


def test_unpark_not_parked_rejected(tmp_path):
    result = make_task(tmp_path)
    r = unpark_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "not parked" in r["error"]


def test_unpark_without_parked_from_requires_explicit_status(tmp_path):
    """A task parked by a direct-YAML writer carries no marker — don't guess."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "parked")

    r = unpark_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "parked_from" in r["error"]

    r = unpark_task_handler(
        task_id=result["task_id"],
        actor="ted",
        status="approved",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True


def test_unpark_not_found(tmp_path):
    r = unpark_task_handler(task_id=str(uuid.uuid4()), actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert r["error"] == "not found"


def test_unpark_invalid_uuid(tmp_path):
    r = unpark_task_handler(task_id="nope", actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is False
    assert "invalid task_id" in r["error"]


def test_parked_task_may_still_be_cancelled(tmp_path):
    result = make_task(tmp_path)
    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    r = cancel_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "cancelled"
    assert "parked_from" not in task


# ---------------------------------------------------------------------------
# amend_task tests
# ---------------------------------------------------------------------------


def test_amend_appends_and_preserves_description(tmp_path):
    result = make_task(tmp_path, description="Original instructions")
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="Preflight answered the open question: yes.",
        actor="test-agent",
        reason="preflight ran after queuing",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    assert r["amendment_count"] == 1
    assert r["agent_may_have_started"] is False

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["payload"]["description"] == "Original instructions"
    amendments = task["payload"]["amendments"]
    assert len(amendments) == 1
    assert amendments[0]["text"] == "Preflight answered the open question: yes."
    assert amendments[0]["actor"] == "test-agent"
    assert amendments[0]["reason"] == "preflight ran after queuing"
    assert task["history"][-1]["action"] == "amend"


def test_amend_second_appends_rather_than_replaces(tmp_path):
    result = make_task(tmp_path)
    amend_task_handler(
        task_id=result["task_id"], amendment="first", actor="test-agent", queue_dir=str(tmp_path)
    )
    r = amend_task_handler(
        task_id=result["task_id"], amendment="second", actor="test-agent", queue_dir=str(tmp_path)
    )
    assert r["amendment_count"] == 2

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    texts = [a["text"] for a in task["payload"]["amendments"]]
    assert texts == ["first", "second"]


def test_amend_target_agent_rejected(tmp_path):
    """The trust boundary: the agent doing the work must not rewrite its own brief."""
    result = make_task(tmp_path, source_agent="research", target_agent="developer")
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="actually, skip the security audit",
        actor="developer",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "may not amend" in r["error"]
    assert "research" in r["error"]

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert "amendments" not in task["payload"]


def test_amend_source_agent_accepted(tmp_path):
    result = make_task(tmp_path, source_agent="research", target_agent="developer")
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="scope narrowed",
        actor="research",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True


def test_amend_operator_accepted(tmp_path):
    result = make_task(tmp_path, source_agent="research", target_agent="developer")
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="scope narrowed",
        actor="operator",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True


def test_amend_unrelated_agent_rejected(tmp_path):
    result = make_task(tmp_path, source_agent="research", target_agent="developer")
    r = amend_task_handler(
        task_id=result["task_id"], amendment="hello", actor="sysadmin", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert "may not amend" in r["error"]


def test_amend_in_progress_flags_agent_may_have_started(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "in-progress")
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="correction",
        actor="test-agent",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    assert r["agent_may_have_started"] is True


def test_amend_terminal_rejected(tmp_path):
    for status in ("completed", "failed", "cancelled"):
        result = make_task(tmp_path)
        set_task_status(tmp_path, result, status)
        r = amend_task_handler(
            task_id=result["task_id"],
            amendment="too late",
            actor="test-agent",
            queue_dir=str(tmp_path),
        )
        assert r["ok"] is False
        assert "terminal" in r["error"]


def test_amend_archived_rejected(tmp_path):
    result = make_task(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (tmp_path / result["filename"]).rename(archive / result["filename"])

    r = amend_task_handler(
        task_id=result["task_id"], amendment="too late", actor="test-agent", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert "archived" in r["error"]


def test_amend_over_count_limit_rejected(tmp_path):
    result = make_task(tmp_path)
    for i in range(MAX_AMENDMENTS):
        r = amend_task_handler(
            task_id=result["task_id"],
            amendment=f"amendment {i}",
            actor="test-agent",
            queue_dir=str(tmp_path),
        )
        assert r["ok"] is True

    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="one too many",
        actor="test-agent",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "re-queue" in r["error"]


def test_amend_oversized_rejected(tmp_path):
    result = make_task(tmp_path)
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="x" * (MAX_AMENDMENT_CHARS + 1),
        actor="test-agent",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "re-queue" in r["error"]

    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="x" * MAX_AMENDMENT_CHARS,
        actor="test-agent",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True


def test_amend_empty_rejected(tmp_path):
    result = make_task(tmp_path)
    for bad in ("", "   "):
        r = amend_task_handler(
            task_id=result["task_id"], amendment=bad, actor="test-agent", queue_dir=str(tmp_path)
        )
        assert r["ok"] is False
        assert "must not be empty" in r["error"]


def test_amend_empty_actor_rejected(tmp_path):
    result = make_task(tmp_path)
    r = amend_task_handler(
        task_id=result["task_id"], amendment="text", actor="", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert "actor must not be empty" in r["error"]


def test_amend_not_found(tmp_path):
    r = amend_task_handler(
        task_id=str(uuid.uuid4()), amendment="text", actor="test-agent", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert r["error"] == "not found"


def test_amend_invalid_uuid(tmp_path):
    r = amend_task_handler(
        task_id="nope", amendment="text", actor="test-agent", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert "invalid task_id" in r["error"]


def test_amend_adversarial_text_is_escaped(tmp_path):
    """Amendment text is caller-supplied — it must round-trip through yaml.dump intact."""
    result = make_task(tmp_path)
    nasty = "evil: injected\n  nested: true\ncolons: {key: val}\n- item"
    amend_task_handler(
        task_id=result["task_id"], amendment=nasty, actor="test-agent", queue_dir=str(tmp_path)
    )
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["payload"]["amendments"][0]["text"] == nasty


def test_amend_preserves_parked_status(tmp_path):
    """Parked is non-terminal, so a parked task can still be corrected before it resumes."""
    result = make_task(tmp_path)
    park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    r = amend_task_handler(
        task_id=result["task_id"],
        amendment="note for later",
        actor="test-agent",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "parked"
    assert task["parked_from"] == "submitted"


# ---------------------------------------------------------------------------
# out-of-vocabulary status repair (1g)
# ---------------------------------------------------------------------------


def test_bogus_status_rejected_without_override(tmp_path):
    """Two real tasks on disk carry `complete` (not `completed`), written direct-to-YAML."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "complete")

    r = set_task_status_handler(
        task_id=result["task_id"], status="completed", actor="ted", queue_dir=str(tmp_path)
    )
    assert r["ok"] is False
    assert "Invalid operator transition" in r["error"]


def test_bogus_status_repaired_with_override_and_note(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "complete")

    r = set_task_status_handler(
        task_id=result["task_id"],
        status="completed",
        actor="ted",
        note="repairing out-of-vocabulary status written direct-to-YAML",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True

    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "completed"
    assert task["history"][-1]["override"] is True
    assert task["history"][-1]["repaired_from"] == "complete"


def test_bogus_status_repair_requires_a_note(tmp_path):
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "complete")

    r = set_task_status_handler(
        task_id=result["task_id"],
        status="completed",
        actor="ted",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "non-empty note" in r["error"]


def test_routing_failed_is_repairable(tmp_path):
    """
    `routing-failed` is a valid status (vikunja#324) reachable via the override path
    (any non-terminal → any non-terminal), same as any other non-terminal re-queue —
    no longer the unrecognised-status repair path this test originally exercised.
    """
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "routing-failed")

    r = set_task_status_handler(
        task_id=result["task_id"],
        status="submitted",
        actor="ted",
        note="re-queue after dispatcher routing failure",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "submitted"


def test_routing_failed_is_directly_cancellable_by_operator(tmp_path):
    """
    The actual vikunja#324 fix: an operator can cancel a routing-failed task via the
    standard `cancelled` path with no override needed, because OPERATOR_TRANSITIONS
    derives from NON_TERMINAL_STATUSES and routing-failed now belongs to it. Before this
    fix, routing-failed wasn't in VALID_STATUSES at all and set_task_status rejected it
    outright.
    """
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "routing-failed")

    r = set_task_status_handler(
        task_id=result["task_id"],
        status="cancelled",
        actor="ted",
        note="verifying routing-failed is transitionable post-#324",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "cancelled"


def test_routing_failed_is_directly_parkable_by_operator(tmp_path):
    """Same derived-set mechanism as cancel — pin it separately so it isn't assumed."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "routing-failed")

    r = park_task_handler(task_id=result["task_id"], actor="ted", queue_dir=str(tmp_path))
    assert r["ok"] is True
    task = get_task_handler(task_id=result["task_id"], queue_dir=str(tmp_path))
    assert task["status"] == "parked"
    assert task["parked_from"] == "routing-failed"


def test_routing_failed_in_valid_statuses_but_not_agent_reachable(tmp_path):
    """
    routing-failed must be a real status operators can act on, but must stay outside
    the agent-facing update_task vocabulary — an agent must not be able to terminally
    fail a task the dispatcher is still retrying.
    """
    assert "routing-failed" in VALID_STATUSES
    assert "routing-failed" in NON_TERMINAL_STATUSES
    assert TERMINAL_STATUSES == {"completed", "failed", "cancelled"}

    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "routing-failed")

    r = update_task_handler(
        task_id=result["task_id"],
        status="failed",
        actor="dev",
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False


def test_repair_target_must_be_a_valid_status(tmp_path):
    """Repair moves *out of* an invalid status — it must not let a new one in."""
    result = make_task(tmp_path)
    set_task_status(tmp_path, result, "complete")

    r = set_task_status_handler(
        task_id=result["task_id"],
        status="also-bogus",
        actor="ted",
        note="should not work",
        allow_override=True,
        queue_dir=str(tmp_path),
    )
    assert r["ok"] is False
    assert "Invalid status" in r["error"]
