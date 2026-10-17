"""
Owner-side task events (operator-panel-2026-09 part 3).

Every transition this server writes produces exactly one event file in `<queue>/.outbox/`,
named and shaped to agent-bus's outbox contract. Three things are tested that would each
fail quietly in production:

- the mapping is exhaustive over every (from, to) status pair, so an unmapped transition is
  a test failure and not a skipped emit;
- every event file passes agent-bus's OWN validator (imported here, never at runtime),
  which is what makes three independent writers of the contract acceptable;
- no event ever carries `payload.description` text, checked with a sentinel.
"""

import json
import os
import uuid
from pathlib import Path

# agent-bus's validator, from the pinned dev dependency. Not importorskip: a skip here would
# turn the contract gate into a test that passes by not running.
import event_vocab
import outbox as bus_outbox
import pytest
import yaml

from src.tools import events
from src.tools.queue import (
    OPERATOR_ACTOR,
    VALID_STATUSES,
    amend_task_handler,
    cancel_task_handler,
    park_task_handler,
    requeue_dead_letter_handler,
    set_task_status_handler,
    submit_task_handler,
    unpark_task_handler,
    update_task_handler,
)

SENTINEL = "SENTINEL-DESCRIPTION-7f3c9a"


def _submit(tmp_path, **kw) -> dict:
    args = dict(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="A task",
        description=f"Do the thing. {SENTINEL}",
        queue_dir=str(tmp_path),
    )
    args.update(kw)
    r = submit_task_handler(**args)
    assert r["ok"], r
    return r


def _events(tmp_path) -> list[dict]:
    d = Path(tmp_path) / events.OUTBOX_DIRNAME
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith("."):
            continue
        body = json.loads(p.read_text())
        # The contract gate: every file this server writes must pass agent-bus's validator.
        bus_outbox.read_and_validate(p, {events.EVENT_SOURCE})
        out.append(body)
    return out


def _set_status(tmp_path, filename, status):
    """Write a status directly, as the dispatcher does."""
    path = Path(tmp_path) / filename
    data = yaml.safe_load(path.read_text())
    data["status"] = status
    path.write_text(yaml.dump(data, sort_keys=False))


def _clear(tmp_path):
    d = Path(tmp_path) / events.OUTBOX_DIRNAME
    for p in d.iterdir():
        p.unlink()


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("to_status", sorted(VALID_STATUSES))
@pytest.mark.parametrize("from_status", [*sorted(VALID_STATUSES), None])
def test_every_transition_maps_to_a_declared_event(from_status, to_status):
    ev = events.event_for(from_status, to_status)
    assert ev in event_vocab.CROSS_AGENT_EVENTS


@pytest.mark.parametrize("action", [events.ACTION_AMEND, events.ACTION_REQUEUE])
def test_actions_map_to_declared_events(action):
    assert events.event_for("approved", "approved", action) in event_vocab.CROSS_AGENT_EVENTS


@pytest.mark.parametrize(
    "from_status,to_status,expected",
    [
        (None, "submitted", "task.submitted"),
        ("submitted", "approved", "task.approved"),
        ("pending-approval", "approved", "task.approved"),
        ("approved", "in-progress", "task.started"),
        ("in-progress", "completed", "task.completed"),
        ("in-progress", "failed", "task.failed"),
        ("approved", "cancelled", "task.cancelled"),
        ("approved", "parked", "task.parked"),
        ("parked", "approved", "task.unparked"),
        ("parked", "in-progress", "task.unparked"),
        ("parked", "cancelled", "task.cancelled"),
        ("routing-failed", "submitted", "task.requeued"),
    ],
)
def test_named_transitions(from_status, to_status, expected):
    assert events.event_for(from_status, to_status) == expected


def test_no_emitter_writes_the_deprecated_dispatched_type():
    produced = {events.event_for(f, t) for f in [*VALID_STATUSES, None] for t in VALID_STATUSES}
    assert "task.dispatched" not in produced


# ---------------------------------------------------------------------------
# One event per transition, from every handler
# ---------------------------------------------------------------------------


def test_submit_emits_submitted(tmp_path):
    r = _submit(tmp_path)
    [e] = _events(tmp_path)
    assert e["event"] == "task.submitted"
    assert e["source"] == "task-queue-mcp"
    assert e["target"] == "developer"
    md = e["metadata"]
    assert md["task_id"] == r["task_id"]
    assert md["to_status"] == "submitted"
    assert "from_status" not in md
    assert md["actor"] == "research"
    assert md["schema"] == 1


def test_a_self_terminal_notify_emits_submitted_then_completed(tmp_path):
    _submit(tmp_path, task_type="notify")
    assert [e["event"] for e in _events(tmp_path)] == ["task.submitted", "task.completed"]


def test_claim_and_close(tmp_path):
    r = _submit(tmp_path)
    _set_status(tmp_path, r["filename"], "approved")
    _clear(tmp_path)
    tid = r["task_id"]
    assert update_task_handler(tid, "in-progress", "developer", queue_dir=str(tmp_path))["ok"]
    assert update_task_handler(tid, "completed", "developer", queue_dir=str(tmp_path))["ok"]
    evs = _events(tmp_path)
    assert [e["event"] for e in evs] == ["task.started", "task.completed"]
    assert [(e["metadata"]["from_status"], e["metadata"]["to_status"]) for e in evs] == [
        ("approved", "in-progress"),
        ("in-progress", "completed"),
    ]


def test_a_refused_transition_emits_nothing(tmp_path):
    r = _submit(tmp_path)
    _clear(tmp_path)
    res = update_task_handler(r["task_id"], "completed", "developer", queue_dir=str(tmp_path))
    assert not res["ok"]
    assert _events(tmp_path) == []


def test_operator_approve_cancel_park_unpark_carry_channel(tmp_path):
    q = str(tmp_path)
    a = _submit(tmp_path)
    b = _submit(tmp_path)
    _clear(tmp_path)
    assert set_task_status_handler(
        a["task_id"], "approved", OPERATOR_ACTOR, queue_dir=q, channel="cloudcli"
    )["ok"]
    assert park_task_handler(a["task_id"], OPERATOR_ACTOR, queue_dir=q, channel="cloudcli")["ok"]
    assert unpark_task_handler(a["task_id"], OPERATOR_ACTOR, queue_dir=q, channel="cloudcli")["ok"]
    assert cancel_task_handler(b["task_id"], OPERATOR_ACTOR, queue_dir=q, channel="cloudcli")["ok"]
    evs = _events(tmp_path)
    assert [e["event"] for e in evs] == [
        "task.approved",
        "task.parked",
        "task.unparked",
        "task.cancelled",
    ]
    assert {e["metadata"]["channel"] for e in evs} == {"cloudcli"}
    assert evs[2]["metadata"]["from_status"] == "parked"
    assert evs[2]["metadata"]["to_status"] == "approved"


def test_an_mcp_write_carries_no_channel(tmp_path):
    r = _submit(tmp_path)
    _clear(tmp_path)
    set_task_status_handler(r["task_id"], "approved", OPERATOR_ACTOR, queue_dir=str(tmp_path))
    [e] = _events(tmp_path)
    assert "channel" not in e["metadata"]


def test_amend_emits_amended_without_changing_status(tmp_path):
    r = _submit(tmp_path)
    _clear(tmp_path)
    assert amend_task_handler(r["task_id"], "also do X", "research", queue_dir=str(tmp_path))["ok"]
    [e] = _events(tmp_path)
    assert e["event"] == "task.amended"
    assert e["metadata"]["from_status"] == e["metadata"]["to_status"] == "submitted"
    assert "also do X" not in json.dumps(e)


def test_requeue_emits_requeued(tmp_path):
    r = _submit(tmp_path)
    src = tmp_path / r["filename"]
    data = yaml.safe_load(src.read_text())
    data["status"] = "failed"
    (tmp_path / "dead-letters").mkdir()
    (tmp_path / "dead-letters" / r["filename"]).write_text(yaml.dump(data, sort_keys=False))
    src.unlink()
    _clear(tmp_path)
    assert requeue_dead_letter_handler(r["task_id"], OPERATOR_ACTOR, queue_dir=str(tmp_path))["ok"]
    [e] = _events(tmp_path)
    assert e["event"] == "task.requeued"
    assert (e["metadata"]["from_status"], e["metadata"]["to_status"]) == ("failed", "submitted")


def test_the_sweep_path_emits_failed_with_actor_and_on_behalf_of(tmp_path):
    r = _submit(tmp_path)
    _set_status(tmp_path, r["filename"], "in-progress")
    _clear(tmp_path)
    res = update_task_handler(
        r["task_id"],
        "failed",
        OPERATOR_ACTOR,
        queue_dir=str(tmp_path),
        on_behalf_of="developer",
        channel="dispatcher",
    )
    assert res["ok"]
    [e] = _events(tmp_path)
    assert e["event"] == "task.failed"
    md = e["metadata"]
    assert (md["actor"], md["on_behalf_of"], md["channel"]) == (
        "operator",
        "developer",
        "dispatcher",
    )


def test_auto_close_emits_started_then_completed_for_the_parent(tmp_path):
    parent = _submit(tmp_path, source_agent="developer", target_agent="security", task_type="audit")
    _set_status(tmp_path, parent["filename"], "approved")
    _clear(tmp_path)
    _submit(
        tmp_path,
        source_agent="security",
        target_agent="developer",
        task_type="notify",
        originating_task_id=parent["task_id"],
    )
    evs = [(e["event"], e["metadata"]["task_id"]) for e in _events(tmp_path)]
    pid = parent["task_id"]
    assert ("task.started", pid) in evs and ("task.completed", pid) in evs


# ---------------------------------------------------------------------------
# What must never be in an event
# ---------------------------------------------------------------------------


def test_no_event_file_contains_description_text(tmp_path):
    q = str(tmp_path)
    r = _submit(tmp_path, summary="short summary")
    _set_status(tmp_path, r["filename"], "approved")
    update_task_handler(r["task_id"], "in-progress", "developer", queue_dir=q)
    amend_task_handler(r["task_id"], f"amended {SENTINEL}", "research", queue_dir=q)
    update_task_handler(
        r["task_id"], "completed", "developer", queue_dir=q, output=f"done {SENTINEL}"
    )
    _submit(tmp_path, task_type="notify")
    d = tmp_path / events.OUTBOX_DIRNAME
    files = [p for p in d.iterdir() if not p.name.startswith(".")]
    assert len(files) == 6
    for p in files:
        assert SENTINEL.encode() not in p.read_bytes()


def test_summary_is_truncated_to_200(tmp_path):
    _submit(tmp_path, summary="s" * 500)
    [e] = _events(tmp_path)
    assert e["summary"] == "s" * 200


def test_long_field_values_are_capped_so_the_event_stays_valid(tmp_path):
    _submit(tmp_path, target_agent="t" * 1000)
    [e] = _events(tmp_path)  # passes agent-bus's validator inside _events
    assert len(e["metadata"]["target_agent"]) == events.FIELD_CHARS


# ---------------------------------------------------------------------------
# Event loss never blocks a queue write
# ---------------------------------------------------------------------------


def test_an_unwritable_outbox_does_not_fail_the_write(tmp_path, caplog):
    (tmp_path / events.OUTBOX_DIRNAME).write_text("a file where the directory should be")
    r = _submit(tmp_path)
    assert r["ok"]
    assert (tmp_path / r["filename"]).exists()
    assert "task event not written" in caplog.text


def test_ensure_outbox_creates_0700(tmp_path):
    assert events.ensure_outbox(str(tmp_path))
    mode = os.stat(tmp_path / events.OUTBOX_DIRNAME).st_mode & 0o777
    assert mode == 0o700
    assert list((tmp_path / events.OUTBOX_DIRNAME).iterdir()) == []


def test_ensure_outbox_reports_failure_and_does_not_raise(tmp_path, caplog):
    (tmp_path / events.OUTBOX_DIRNAME).write_text("not a dir")
    assert events.ensure_outbox(str(tmp_path)) is False
    assert "not writable" in caplog.text


def test_queue_readers_do_not_see_the_outbox(tmp_path):
    from src.tools.queue import list_tasks_handler

    _submit(tmp_path)
    assert len(list_tasks_handler(queue_dir=str(tmp_path))) == 1


# ---------------------------------------------------------------------------
# build_name / audit_round
# ---------------------------------------------------------------------------


def test_build_name_and_audit_round_are_stored_and_emitted(tmp_path):
    r = _submit(tmp_path, task_type="audit", build_name="op-panel-p3_x", audit_round=2)
    from src.tools.queue import get_task_handler

    t = get_task_handler(r["task_id"], queue_dir=str(tmp_path))
    assert (t["build_name"], t["audit_round"]) == ("op-panel-p3_x", 2)
    [e] = _events(tmp_path)
    assert (e["metadata"]["build_name"], e["metadata"]["audit_round"]) == ("op-panel-p3_x", 2)


def test_both_fields_are_optional_and_absent_by_default(tmp_path):
    r = _submit(tmp_path)
    data = yaml.safe_load((tmp_path / r["filename"]).read_text())
    assert "build_name" not in data and "audit_round" not in data


@pytest.mark.parametrize("name", ["has.dot", "has/slash", "", "x" * 129, "unicodé", "sp ace", 7])
def test_bad_build_names_are_refused(tmp_path, name):
    r = submit_task_handler(
        "research", "developer", "build", "s", "d", queue_dir=str(tmp_path), build_name=name
    )
    assert not r["ok"] and "build_name" in r["error"]


@pytest.mark.parametrize("rnd", [0, 21, -1, True, "2", 1.5])
def test_bad_audit_rounds_are_refused(tmp_path, rnd):
    r = submit_task_handler(
        "research", "security", "audit", "s", "d", queue_dir=str(tmp_path), audit_round=rnd
    )
    assert not r["ok"] and "audit_round" in r["error"]


@pytest.mark.parametrize("task_type", ["audit", "ticket_audit"])
def test_audit_round_is_allowed_for_audits(tmp_path, task_type):
    r = submit_task_handler(
        "research", "security", task_type, "s", "d", queue_dir=str(tmp_path), audit_round=20
    )
    assert r["ok"], r


def test_audit_round_is_refused_off_an_audit(tmp_path):
    r = submit_task_handler(
        "research", "developer", "build", "s", "d", queue_dir=str(tmp_path), audit_round=1
    )
    assert not r["ok"] and "only valid" in r["error"]


def test_a_refused_submit_writes_no_task_and_no_event(tmp_path):
    submit_task_handler(
        "research", "developer", "build", "s", "d", queue_dir=str(tmp_path), build_name="a.b"
    )
    assert list(tmp_path.glob("*.yml")) == []
    assert _events(tmp_path) == []


def test_the_writer_matches_the_reference_file_name(tmp_path):
    _submit(tmp_path)
    [p] = [p for p in (tmp_path / events.OUTBOX_DIRNAME).iterdir()]
    m = bus_outbox.NAME_RE.match(p.name)
    assert m and uuid.UUID(m.group(2)).version == 4


def test_stamps_are_strictly_increasing_even_when_the_clock_is_not(monkeypatch):
    frozen = events.datetime(2026, 10, 16, tzinfo=events.UTC)

    class _Clock(events.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(events, "datetime", _Clock)
    monkeypatch.setattr(events, "_last_stamp", None)
    stamps = [events._next_stamp() for _ in range(5)]
    assert stamps == sorted(stamps) and len(set(stamps)) == 5


def test_a_notify_drains_submitted_before_completed(tmp_path):
    _submit(tmp_path, task_type="notify")
    names = sorted(p.name for p in (tmp_path / events.OUTBOX_DIRNAME).iterdir())
    bodies = [json.loads((tmp_path / events.OUTBOX_DIRNAME / n).read_text()) for n in names]
    assert [b["event"] for b in bodies] == ["task.submitted", "task.completed"]
