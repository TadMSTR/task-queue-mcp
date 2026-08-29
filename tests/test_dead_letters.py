"""
Dead-letter visibility and recovery (vikunja#557).

The bug these guard against is not a crash — it is silence. `dead-letters/` is written by
task-dispatcher when a task exhausts its routing retries, and until this release no tool
in this server could see into it: `get_task` searched the queue root then `archive/` and
answered `not found`, `list_tasks` globbed the root, `/queue/summary` counted the root.
Seventeen tasks accumulated there between 2026-05-29 and 2026-07-25 — every one a security
audit request — and the only notice any of them got was a single Matrix message at the
moment it was dropped.

Two of these tests are the ones that would actually have caught the shipped-but-useless
version of this feature:

  * test_expired_dead_letter_still_listed — every dead letter carries `failed`, a terminal
    status, and the real ones are months past their ttl_days. Without the TTL exemption,
    `include_dead_letters=True` returns an empty list against the live queue and the
    feature reads as "there are none".
  * test_requeue_does_not_persist_internal_keys — the loader now annotates each record with
    `_location`, and the handlers hand the loaded dict straight to the writer.
"""

from datetime import UTC, datetime, timedelta

import pytest
import yaml

import src.auth as auth_mod
from src.tools.queue import (
    DEAD_LETTER_DIRNAME,
    LOCATION_ARCHIVE,
    LOCATION_DEAD_LETTER,
    LOCATION_QUEUE,
    amend_task_handler,
    count_dead_letters,
    get_task_handler,
    list_tasks_handler,
    requeue_dead_letter_handler,
    set_task_status_handler,
    submit_task_handler,
    unpark_task_handler,
    update_task_handler,
)

OPERATOR = "operator"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _submit(tmp_path, **kwargs) -> dict:
    defaults = dict(
        source_agent="developer",
        target_agent="security",
        task_type="audit",
        summary="Security audit requested: some-build",
        description="Audit request",
        queue_dir=str(tmp_path),
    )
    defaults.update(kwargs)
    return submit_task_handler(**defaults)


def _dead_letter(
    tmp_path, result: dict, reason="Invalid or missing build_name in payload: 'unknown'"
):
    """
    Move a submitted task into dead-letters/ exactly as task-dispatcher's
    move_to_dead_letter does: status `failed`, a `failed_reason` block, file relocated.
    """
    src = tmp_path / result["filename"]
    data = yaml.safe_load(src.read_text())
    data["status"] = "failed"
    data["retry_policy"] = {
        "next_retry_at": "2026-05-29T12:34:00.086069+00:00",
        "retry_count": 3,
        "last_failure_reason": reason,
    }
    data["failed_reason"] = {
        "timestamp": "2026-05-29T12:34:00.126757+00:00",
        "reason": reason,
        "retry_count": 3,
    }
    dead_dir = tmp_path / DEAD_LETTER_DIRNAME
    dead_dir.mkdir(exist_ok=True)
    (dead_dir / result["filename"]).write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False)
    )
    src.unlink()
    return result["task_id"]


def _archive(tmp_path, result: dict):
    src = tmp_path / result["filename"]
    arc = tmp_path / "archive"
    arc.mkdir(exist_ok=True)
    src.rename(arc / result["filename"])
    return result["task_id"]


def _age_out(tmp_path, result: dict, subdir=DEAD_LETTER_DIRNAME):
    """
    Backdate a record past its ttl_days, as all seventeen live ones are.

    `created` is written as a real datetime, not an ISO string. The TTL filter guards on
    `isinstance(created, datetime)`, and the on-disk records parse back as datetimes
    because the dispatcher yaml.dumps them unquoted — verified against
    dead-letters/20260529-115542-96e8d44f.yml. Backdating with a string would leave every
    TTL assertion here passing for the wrong reason: the filter would never fire at all,
    and the exemption under test would be doing nothing.
    """
    path = tmp_path / subdir / result["filename"]
    data = yaml.safe_load(path.read_text())
    data["created"] = datetime.now(UTC) - timedelta(days=90)
    data["ttl_days"] = 14
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


# --------------------------------------------------------------------------- #
# get_task resolves dead-letters/
# --------------------------------------------------------------------------- #


def test_get_task_resolves_a_dead_letter(tmp_path):
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))

    assert task.get("ok") is not False
    assert task["id"] == tid


def test_get_task_keeps_the_failed_reason_block(tmp_path):
    """The reason is the whole value of the record — all 17 share one."""
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))

    assert task["failed_reason"]["reason"] == (
        "Invalid or missing build_name in payload: 'unknown'"
    )
    assert task["failed_reason"]["retry_count"] == 3


def test_get_task_marks_which_directory_the_record_came_from(tmp_path):
    """
    A caller must be able to tell a dead letter from live work without inspecting paths —
    which it cannot do anyway, since `_path` never leaves this module.
    """
    live = _submit(tmp_path)
    dead = _dead_letter(tmp_path, _submit(tmp_path))
    archived = _archive(tmp_path, _submit(tmp_path))

    q = str(tmp_path)
    assert get_task_handler(task_id=live["task_id"], queue_dir=q)["queue_location"] == (
        LOCATION_QUEUE
    )
    assert get_task_handler(task_id=dead, queue_dir=q)["queue_location"] == LOCATION_DEAD_LETTER
    assert get_task_handler(task_id=archived, queue_dir=q)["queue_location"] == LOCATION_ARCHIVE


def test_get_task_still_reports_not_found_for_an_unknown_id(tmp_path):
    _dead_letter(tmp_path, _submit(tmp_path))
    unknown = "00000000-0000-4000-8000-000000000000"

    assert get_task_handler(task_id=unknown, queue_dir=str(tmp_path)) == {
        "ok": False,
        "error": "not found",
    }


def test_internal_keys_never_leave_the_module(tmp_path):
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))

    assert not [k for k in task if k.startswith("_")]


# --------------------------------------------------------------------------- #
# list_tasks — opt-in only
# --------------------------------------------------------------------------- #


def test_dead_letters_are_absent_from_the_default_listing(tmp_path):
    """
    THE GATE. Every agent's work sweep is a list_tasks call. A dead letter is a task
    nothing can route; folding it into the default listing hands each agent a backlog it
    cannot act on.
    """
    live = _submit(tmp_path)
    dead = _dead_letter(tmp_path, _submit(tmp_path))

    ids = [t["id"] for t in list_tasks_handler(queue_dir=str(tmp_path))]

    assert ids == [live["task_id"]]
    assert dead not in ids


def test_include_archived_alone_does_not_pull_in_dead_letters(tmp_path):
    """Two directories, two opt-ins. They are not the same category of record."""
    dead = _dead_letter(tmp_path, _submit(tmp_path))
    archived = _archive(tmp_path, _submit(tmp_path))

    ids = [t["id"] for t in list_tasks_handler(include_archived=True, queue_dir=str(tmp_path))]

    assert archived in ids
    assert dead not in ids


def test_include_dead_letters_returns_them(tmp_path):
    dead = _dead_letter(tmp_path, _submit(tmp_path))

    tasks = list_tasks_handler(include_dead_letters=True, queue_dir=str(tmp_path))

    assert [t["id"] for t in tasks] == [dead]
    assert tasks[0]["queue_location"] == LOCATION_DEAD_LETTER


def test_include_dead_letters_alone_does_not_pull_in_the_archive(tmp_path):
    dead = _dead_letter(tmp_path, _submit(tmp_path))
    archived = _archive(tmp_path, _submit(tmp_path))

    ids = [t["id"] for t in list_tasks_handler(include_dead_letters=True, queue_dir=str(tmp_path))]

    assert ids == [dead]
    assert archived not in ids


def test_expired_dead_letter_still_listed(tmp_path):
    """
    The trap. A dead letter carries `failed` — terminal — so the TTL filter that ages out
    finished work would age out every one of them: the newest of the seventeen is past its
    ttl_days, and the oldest by nearly three months. Without the exemption this feature
    ships green and reports an empty dead-letter queue on the live host.
    """
    r = _submit(tmp_path, ttl_days=14)
    dead = _dead_letter(tmp_path, r)
    _age_out(tmp_path, r)

    ids = [t["id"] for t in list_tasks_handler(include_dead_letters=True, queue_dir=str(tmp_path))]

    assert ids == [dead]


def test_expired_terminal_task_in_the_queue_root_still_ages_out(tmp_path):
    """The exemption is scoped to dead-letters/ — it does not disable the TTL filter."""
    r = _submit(tmp_path, ttl_days=14)
    path = tmp_path / r["filename"]
    data = yaml.safe_load(path.read_text())
    data["status"] = "completed"
    data["created"] = datetime.now(UTC) - timedelta(days=90)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    tasks = list_tasks_handler(include_dead_letters=True, queue_dir=str(tmp_path))

    assert tasks == []


def test_dead_letters_survive_the_limit_on_a_busy_queue(tmp_path):
    """
    The defect the live queue caught, and the reason dead letters sort first.

    A dead letter is among the oldest records in the queue by construction — it got there
    by exhausting retries. Under a plain created-descending sort it lands behind every live
    task, and `filtered[:limit]` throws it away: run against the real queue,
    `include_dead_letters=True, limit=200` returned 200 rows and zero dead letters. The
    flag existed, the exemption worked, and the answer was still "there are none".
    """
    dead = _dead_letter(tmp_path, _submit(tmp_path))
    for _ in range(5):
        _submit(tmp_path)

    tasks = list_tasks_handler(include_dead_letters=True, limit=2, queue_dir=str(tmp_path))

    assert dead in [t["id"] for t in tasks]


def test_live_records_keep_their_created_descending_order(tmp_path):
    """The dead-letter-first key must not disturb the ordering of everything else."""
    ids = [_submit(tmp_path)["task_id"] for _ in range(3)]
    _dead_letter(tmp_path, _submit(tmp_path))

    live = [
        t["id"]
        for t in list_tasks_handler(include_dead_letters=True, queue_dir=str(tmp_path))
        if t["queue_location"] == LOCATION_QUEUE
    ]

    assert live == list(reversed(ids))


def test_dead_letters_respect_the_other_filters(tmp_path):
    a = _dead_letter(tmp_path, _submit(tmp_path, target_agent="security"))
    _dead_letter(tmp_path, _submit(tmp_path, target_agent="writer"))

    tasks = list_tasks_handler(
        target_agent="security", include_dead_letters=True, queue_dir=str(tmp_path)
    )

    assert [t["id"] for t in tasks] == [a]


def test_live_records_are_labelled_queue(tmp_path):
    _submit(tmp_path)

    assert list_tasks_handler(queue_dir=str(tmp_path))[0]["queue_location"] == LOCATION_QUEUE


# --------------------------------------------------------------------------- #
# count_dead_letters (the /queue/summary key)
# --------------------------------------------------------------------------- #


def test_count_dead_letters_is_zero_without_the_directory(tmp_path):
    _submit(tmp_path)

    assert count_dead_letters(str(tmp_path)) == 0


def test_count_dead_letters_counts_them(tmp_path):
    for _ in range(3):
        _dead_letter(tmp_path, _submit(tmp_path))

    assert count_dead_letters(str(tmp_path)) == 3


def test_count_dead_letters_ignores_tmp_files(tmp_path):
    _dead_letter(tmp_path, _submit(tmp_path))
    (tmp_path / DEAD_LETTER_DIRNAME / "half-written.yml.tmp").write_text("id: x\n")

    assert count_dead_letters(str(tmp_path)) == 1


# --------------------------------------------------------------------------- #
# Mutating handlers refuse a dead letter, and say why
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda q, tid: update_task_handler(
                task_id=tid, status="in-progress", actor="security", queue_dir=q
            ),
            id="update_task",
        ),
        pytest.param(
            lambda q, tid: set_task_status_handler(
                task_id=tid, status="approved", actor=OPERATOR, queue_dir=q
            ),
            id="set_task_status",
        ),
        pytest.param(
            lambda q, tid: amend_task_handler(
                task_id=tid, amendment="more", actor="developer", queue_dir=q
            ),
            id="amend_task",
        ),
        pytest.param(
            lambda q, tid: unpark_task_handler(task_id=tid, actor=OPERATOR, queue_dir=q),
            id="unpark_task",
        ),
    ],
)
def test_mutating_handlers_refuse_a_dead_letter_by_name(tmp_path, call):
    """
    The mutating handlers do not load dead-letters/ at all — that absence IS the gate that
    keeps a dead letter unreachable from every ordinary transition. What is asserted here
    is that the refusal says so, rather than answering `not found` for a record the caller
    can plainly see via get_task.
    """
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    out = call(str(tmp_path), tid)

    assert out["ok"] is False
    assert "dead-lettered" in out["error"]
    assert out["error"] != "not found"


def test_a_genuinely_unknown_id_still_reports_not_found(tmp_path):
    _dead_letter(tmp_path, _submit(tmp_path))
    unknown = "00000000-0000-4000-8000-000000000000"

    out = update_task_handler(
        task_id=unknown, status="in-progress", actor="security", queue_dir=str(tmp_path)
    )

    assert out["error"] == "not found"


def test_a_refused_mutation_leaves_the_dead_letter_untouched(tmp_path):
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)
    before = (tmp_path / DEAD_LETTER_DIRNAME / r["filename"]).read_text()

    set_task_status_handler(task_id=tid, status="approved", actor=OPERATOR, queue_dir=str(tmp_path))

    assert (tmp_path / DEAD_LETTER_DIRNAME / r["filename"]).read_text() == before
    assert not (tmp_path / r["filename"]).exists()


# --------------------------------------------------------------------------- #
# requeue
# --------------------------------------------------------------------------- #


def test_requeue_moves_the_file_back_to_the_queue_root(tmp_path):
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)

    out = requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert out["ok"] is True
    assert out["filename"] == r["filename"]
    assert out["requeued_from"] == LOCATION_DEAD_LETTER
    assert (tmp_path / r["filename"]).is_file()
    assert not (tmp_path / DEAD_LETTER_DIRNAME / r["filename"]).exists()


def test_requeue_resets_the_record_for_another_attempt(tmp_path):
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["status"] == "submitted"
    assert "failed_reason" not in task
    assert task["retry_policy"] == {"next_retry_at": None, "retry_count": 0}
    assert task["queue_location"] == LOCATION_QUEUE


def test_requeue_preserves_created(tmp_path):
    """
    When the work was first asked for is the record. Refreshing `created` would make a
    three-month-old dropped audit look new, which is the flavour of tidiness that made
    this backlog invisible in the first place.
    """
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)
    _age_out(tmp_path, r)
    before = get_task_handler(task_id=tid, queue_dir=str(tmp_path))["created"]

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["created"] == before


def test_requeue_appends_an_audited_history_entry(tmp_path):
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    requeue_dead_letter_handler(
        task_id=tid, actor=OPERATOR, note="build_name fixed", queue_dir=str(tmp_path)
    )

    entry = get_task_handler(task_id=tid, queue_dir=str(tmp_path))["history"][-1]
    assert entry["status"] == "submitted"
    assert entry["actor"] == OPERATOR
    assert entry["note"] == "build_name fixed"
    assert entry["action"] == "requeue"
    assert entry["requeued_from"] == LOCATION_DEAD_LETTER


def test_requeue_records_why_it_had_died(tmp_path):
    """Clearing failed_reason must not erase it — a second drop should not read as a first."""
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    entry = get_task_handler(task_id=tid, queue_dir=str(tmp_path))["history"][-1]
    assert entry["cleared_failed_reason"] == ("Invalid or missing build_name in payload: 'unknown'")


def test_requeued_task_rejoins_the_default_listing(tmp_path):
    """The point of the whole path: it is work again, in the sweep an agent actually runs."""
    tid = _dead_letter(tmp_path, _submit(tmp_path))
    assert list_tasks_handler(queue_dir=str(tmp_path)) == []

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert [t["id"] for t in list_tasks_handler(queue_dir=str(tmp_path))] == [tid]
    assert count_dead_letters(str(tmp_path)) == 0


def test_requeued_task_can_then_be_transitioned_normally(tmp_path):
    tid = _dead_letter(tmp_path, _submit(tmp_path))
    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    approved = set_task_status_handler(
        task_id=tid, status="approved", actor=OPERATOR, queue_dir=str(tmp_path)
    )

    assert approved["ok"] is True
    assert (
        update_task_handler(
            task_id=tid, status="in-progress", actor="security", queue_dir=str(tmp_path)
        )["ok"]
        is True
    )


def test_requeue_does_not_persist_internal_keys(tmp_path):
    """
    `_location` is attached by the loader and the handlers hand the loaded dict straight
    to the writer. The previous writer stripped `_path` by name; anything else the loader
    added would have been serialised into the YAML on the first transition after it.
    """
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    on_disk = yaml.safe_load((tmp_path / r["filename"]).read_text())
    assert not [k for k in on_disk if k.startswith("_")]


def test_requeue_leaves_alert_state_to_the_dispatcher(tmp_path):
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)
    path = tmp_path / DEAD_LETTER_DIRNAME / r["filename"]
    data = yaml.safe_load(path.read_text())
    data["alert_state"] = {"alert_count": 2, "first_alerted_at": "2026-06-01T00:00:00+00:00"}
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["alert_state"]["alert_count"] == 2


# ── requeue refusals ────────────────────────────────────────────────────


def test_requeue_will_not_resurrect_a_failed_task_from_the_live_queue(tmp_path):
    """
    Terminal immutability is not weakened by this handler — it is scoped to one directory.
    A `failed` task in the queue root is an agent's judgement that the work is over, and
    stays that way.
    """
    r = _submit(tmp_path)
    path = tmp_path / r["filename"]
    data = yaml.safe_load(path.read_text())
    data["status"] = "failed"
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    out = requeue_dead_letter_handler(task_id=r["task_id"], actor=OPERATOR, queue_dir=str(tmp_path))

    assert out == {"ok": False, "error": "not found"}
    assert get_task_handler(task_id=r["task_id"], queue_dir=str(tmp_path))["status"] == "failed"


def test_requeue_will_not_resurrect_an_archived_task(tmp_path):
    r = _submit(tmp_path)
    tid = _archive(tmp_path, r)

    out = requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert out["ok"] is False
    assert (tmp_path / "archive" / r["filename"]).is_file()
    assert not (tmp_path / r["filename"]).exists()


def test_requeue_refuses_a_filename_collision(tmp_path):
    """Recovering dead work must never overwrite live work."""
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)
    (tmp_path / r["filename"]).write_text("id: someone-else\n")

    out = requeue_dead_letter_handler(task_id=tid, actor=OPERATOR, queue_dir=str(tmp_path))

    assert out["ok"] is False
    assert "already exists" in out["error"]
    assert (tmp_path / r["filename"]).read_text() == "id: someone-else\n"
    assert (tmp_path / DEAD_LETTER_DIRNAME / r["filename"]).is_file()


def test_requeue_rejects_a_malformed_id(tmp_path):
    out = requeue_dead_letter_handler(task_id="not-a-uuid", actor=OPERATOR, queue_dir=str(tmp_path))

    assert out == {"ok": False, "error": "invalid task_id format"}


def test_requeue_requires_an_actor(tmp_path):
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    out = requeue_dead_letter_handler(task_id=tid, actor="   ", queue_dir=str(tmp_path))

    assert out["ok"] is False
    assert "actor" in out["error"]


def test_requeue_of_an_unknown_id_is_not_found(tmp_path):
    out = requeue_dead_letter_handler(
        task_id="00000000-0000-4000-8000-000000000000",
        actor=OPERATOR,
        queue_dir=str(tmp_path),
    )

    assert out == {"ok": False, "error": "not found"}


def test_requeue_reads_the_queue_dir_from_the_environment(tmp_path, monkeypatch):
    tid = _dead_letter(tmp_path, _submit(tmp_path))
    monkeypatch.setenv("TASK_QUEUE_DIR", str(tmp_path))

    assert requeue_dead_letter_handler(task_id=tid, actor=OPERATOR)["ok"] is True


# --------------------------------------------------------------------------- #
# The operator gate
# --------------------------------------------------------------------------- #


def test_requeue_tool_is_unreachable_with_an_agent_identity(tmp_path, monkeypatch):
    """
    Same gate as set_task_status. If an agent could requeue its own dead letters, a
    routing bug that drops a task becomes an agent-driven retry loop and the dispatcher's
    retry ceiling bounds nothing.
    """
    import importlib

    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    r = _submit(tmp_path)
    tid = _dead_letter(tmp_path, r)

    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "security")
    out = srv.requeue_dead_letter(task_id=tid, actor="security")

    assert out["ok"] is False
    assert "operator-only" in out["error"]
    assert (tmp_path / DEAD_LETTER_DIRNAME / r["filename"]).is_file()
    assert not (tmp_path / r["filename"]).exists()


def test_requeue_gate_does_not_depend_on_the_actor_argument(tmp_path, monkeypatch):
    """Claiming to be the operator must not talk an agent past the gate."""
    import importlib

    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "security")
    out = srv.requeue_dead_letter(task_id=tid, actor=OPERATOR)

    assert out["ok"] is False
    assert "operator-only" in out["error"]


def test_requeue_tool_proceeds_on_the_operator_surface(tmp_path, monkeypatch):
    """No resolved identity means the control routes, stdio or a test — the gate passes."""
    import importlib

    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    tid = _dead_letter(tmp_path, _submit(tmp_path))

    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: None)

    assert srv.requeue_dead_letter(task_id=tid, actor=OPERATOR)["ok"] is True


def test_requeue_tool_is_registered_and_documented_as_operator_only(tmp_path):
    import src.server as srv

    assert "OPERATOR ONLY" in srv.requeue_dead_letter.__doc__
