"""
Tests for the HTTP control API (FastMCP custom routes on the shared port).

Covers the happy path, transition validation surfacing through HTTP status codes,
and — critically — the shared-secret gate (missing/wrong secret → 401, no mutation).
Exercised end-to-end via Starlette's TestClient against mcp.http_app().
"""

import importlib

import pytest
import yaml
from starlette.testclient import TestClient

from src.tools.queue import get_task_handler, submit_task_handler, update_task_handler

SECRET = "test-secret-value"
AUTH = {"X-Task-Queue-Secret": SECRET}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Reload src.server with QUEUE_DIR -> tmp and a known API secret."""
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", SECRET)
    return srv, tmp_path


@pytest.fixture
def client(env):
    srv, _ = env
    with TestClient(srv.mcp.http_app()) as c:
        yield c


def _dead_letter(tmp_path, result):
    """Relocate a task to dead-letters/ the way task-dispatcher's move_to_dead_letter does."""
    src = tmp_path / result["filename"]
    data = yaml.safe_load(src.read_text())
    data["status"] = "failed"
    data["failed_reason"] = {
        "timestamp": "2026-05-29T12:34:00.126757+00:00",
        "reason": "Invalid or missing build_name in payload: 'unknown'",
        "retry_count": 3,
    }
    dead = tmp_path / "dead-letters"
    dead.mkdir(exist_ok=True)
    (dead / result["filename"]).write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False)
    )
    src.unlink()
    return result["task_id"]


def _seed_result(tmp_path):
    """`_seed` returns only the id; the dead-letter helpers need the filename too."""
    return submit_task_handler(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )


def _seed(tmp_path, status=None):
    r = submit_task_handler(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
        queue_dir=str(tmp_path),
    )
    if status:
        path = tmp_path / r["filename"]
        with open(path) as f:
            data = yaml.safe_load(f)
        data["status"] = status
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return r["task_id"]


# ── auth gate ──────────────────────────────────────────────────────────


def test_approve_missing_secret_rejected(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(f"/tasks/{tid}/approve", json={"actor": "ted"})
    assert resp.status_code == 401
    # No mutation occurred
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"


def test_approve_wrong_secret_rejected(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(
        f"/tasks/{tid}/approve",
        headers={"X-Task-Queue-Secret": "wrong"},
        json={"actor": "ted"},
    )
    assert resp.status_code == 401
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"


def test_no_secret_configured_fails_closed(env, tmp_path, monkeypatch):
    srv, qdir = env
    monkeypatch.delenv("TASK_QUEUE_API_SECRET", raising=False)
    tid = _seed(qdir)
    with TestClient(srv.mcp.http_app()) as c:
        resp = c.post(f"/tasks/{tid}/approve", headers=AUTH, json={"actor": "ted"})
    assert resp.status_code == 401


def test_non_ascii_secret_header_rejected_cleanly(env, client):
    """A non-ASCII secret header must yield a clean 401, not a 500 (audit L-02).

    Sent as latin-1 bytes — Starlette decodes request headers as latin-1, so the server
    sees a non-ASCII str, which would make a str-based hmac.compare_digest raise TypeError.
    """
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(
        f"/tasks/{tid}/approve",
        headers={"X-Task-Queue-Secret": "wrong-café".encode("latin-1")},
        json={"actor": "ted"},
    )
    assert resp.status_code == 401
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"


# ── happy paths ────────────────────────────────────────────────────────


def test_approve_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(f"/tasks/{tid}/approve", headers=AUTH, json={"actor": "ted"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "approved"


def test_cancel_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path, status="approved")
    resp = client.post(f"/tasks/{tid}/cancel", headers=AUTH, json={"actor": "ted", "note": "stale"})
    assert resp.status_code == 200
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "cancelled"


def test_status_override_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path, status="approved")
    resp = client.post(
        f"/tasks/{tid}/status",
        headers=AUTH,
        json={"status": "in-progress", "actor": "ted", "note": "advance", "allow_override": True},
    )
    assert resp.status_code == 200
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "in-progress"


def test_park_then_unpark_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path, status="approved")

    p = client.post(f"/tasks/{tid}/park", headers=AUTH, json={"actor": "ted"})
    assert p.status_code == 200
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "parked"
    # No subdirectory is ever created — the file stays put.
    assert not (tmp_path / "quarantine").exists()

    u = client.post(f"/tasks/{tid}/unpark", headers=AUTH, json={"actor": "ted"})
    assert u.status_code == 200
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "approved"


def test_park_requires_secret(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(f"/tasks/{tid}/park", json={"actor": "ted"})
    assert resp.status_code == 401
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"


def test_unpark_explicit_status_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    client.post(f"/tasks/{tid}/park", headers=AUTH, json={"actor": "ted"})
    u = client.post(
        f"/tasks/{tid}/unpark", headers=AUTH, json={"actor": "ted", "status": "approved"}
    )
    assert u.status_code == 200
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "approved"


def test_amend_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(
        f"/tasks/{tid}/amend",
        headers=AUTH,
        json={"amendment": "scope narrowed", "actor": "research", "reason": "preflight"},
    )
    assert resp.status_code == 200
    assert resp.json()["amendment_count"] == 1

    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["payload"]["description"] == "d"
    assert task["payload"]["amendments"][0]["text"] == "scope narrowed"


def test_amend_requires_secret(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(f"/tasks/{tid}/amend", json={"amendment": "x", "actor": "research"})
    assert resp.status_code == 401
    assert "amendments" not in get_task_handler(task_id=tid, queue_dir=str(tmp_path))["payload"]


def test_amend_ignores_a_body_supplied_actor(env, client):
    """
    Retargeted from test_amend_target_agent_rejected_400, which posted actor="developer"
    (the target agent) and asserted a 400 from the handler's own authorization rule. Now
    that the actor is pinned to `operator` on these routes the body's actor never reaches
    the handler, so that scenario is no longer expressible over HTTP. The rule it covered
    still lives at test_queue.py::test_amend_target_agent_rejected.

    What matters at this layer is stronger: a caller cannot choose an identity here at all.
    Passing the target agent's name must be neither honoured nor rejected — it must be
    ignored, and the amendment recorded as the operator's.
    """
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(
        f"/tasks/{tid}/amend",
        headers=AUTH,
        json={"amendment": "skip the audit", "actor": "developer"},
    )
    assert resp.status_code == 200
    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["payload"]["amendments"][0]["actor"] == "operator"


def test_amend_pins_actor_to_operator(env, client):
    """The control API is operator-facing; an omitted actor must not become the target."""
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(f"/tasks/{tid}/amend", headers=AUTH, json={"amendment": "from the UI"})
    assert resp.status_code == 200
    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["payload"]["amendments"][0]["actor"] == "operator"


def test_retired_quarantine_route_is_gone(env, client):
    """404, not 401 — the route itself must not exist after 0.4.0."""
    _, tmp_path = env
    tid = _seed(tmp_path)
    for route in ("quarantine", "restore"):
        resp = client.post(f"/tasks/{tid}/{route}", headers=AUTH, json={"actor": "ted"})
        assert resp.status_code == 404, f"/tasks/<id>/{route} should no longer be routed"


# ── queue summary ──────────────────────────────────────────────────────


def test_queue_summary_ok(env, client):
    _, tmp_path = env
    _seed(tmp_path)
    _seed(tmp_path, status="approved")
    _seed(tmp_path, status="completed")

    resp = client.get("/queue/summary", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["counts"] == {"submitted": 1, "approved": 1, "completed": 1}
    assert body["active"] == 2
    assert body["total"] == 3


def test_queue_summary_buckets_unknown_statuses(env, client):
    """Out-of-vocabulary records must stay visible, not silently vanish from the count."""
    _, tmp_path = env
    _seed(tmp_path, status="complete")
    _seed(tmp_path, status="parked")

    body = client.get("/queue/summary", headers=AUTH).json()
    assert body["counts"]["unknown"] == 1
    assert body["counts"]["parked"] == 1
    assert body["active"] == 1
    assert body["total"] == 2


def test_queue_summary_counts_routing_failed_as_active(env, client):
    """
    routing-failed is now a real VALID_STATUSES entry (vikunja#324), not an unknown bucket
    — it must be counted by name and included in `active`.
    """
    _, tmp_path = env
    _seed(tmp_path, status="routing-failed")
    _seed(tmp_path, status="parked")

    body = client.get("/queue/summary", headers=AUTH).json()
    assert body["counts"]["routing-failed"] == 1
    assert body["counts"]["parked"] == 1
    assert "unknown" not in body["counts"]
    assert body["active"] == 2
    assert body["total"] == 2


def test_queue_summary_requires_secret(env, client):
    resp = client.get("/queue/summary")
    assert resp.status_code == 401


def test_queue_summary_reports_dead_letters(env, client):
    """
    The count that did not exist. Seventeen dead letters were absent from every number
    this route produced, which is precisely how they accumulated for three months without
    anyone noticing. (vikunja#557)
    """
    _, tmp_path = env
    _seed(tmp_path)
    for _ in range(3):
        _dead_letter(tmp_path, _seed_result(tmp_path))

    body = client.get("/queue/summary", headers=AUTH).json()

    assert body["dead_letters"] == 3


def test_queue_summary_keeps_dead_letters_out_of_the_status_histogram(env, client):
    """
    Every dead letter carries `failed`. Folding them into `counts` would bury them among
    genuinely finished work — the same invisibility, one field along. `counts`, `active`
    and `total` describe the ACTIVE queue; `dead_letters` is its own key.
    """
    _, tmp_path = env
    _seed(tmp_path)
    _dead_letter(tmp_path, _seed_result(tmp_path))

    body = client.get("/queue/summary", headers=AUTH).json()

    assert body["counts"] == {"submitted": 1}
    assert body["total"] == 1
    assert body["active"] == 1
    assert body["dead_letters"] == 1


def test_queue_summary_reports_zero_dead_letters_when_there_are_none(env, client):
    _, tmp_path = env
    _seed(tmp_path)

    assert client.get("/queue/summary", headers=AUTH).json()["dead_letters"] == 0


# ── requeue ────────────────────────────────────────────────────────────


def test_requeue_moves_the_task_back_into_the_queue(env, client):
    _, tmp_path = env
    r = _seed_result(tmp_path)
    tid = _dead_letter(tmp_path, r)

    resp = client.post(f"/tasks/{tid}/requeue", headers=AUTH, json={"note": "root cause fixed"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert (tmp_path / r["filename"]).is_file()
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"


def test_requeue_records_the_operator_as_the_actor(env, client):
    """
    The route pins `actor` rather than reading it from the body, like every other mutation
    route here — a caller must not be able to attribute a resurrection to someone else.
    """
    _, tmp_path = env
    tid = _dead_letter(tmp_path, _seed_result(tmp_path))

    client.post(f"/tasks/{tid}/requeue", headers=AUTH, json={"actor": "security", "note": "n"})

    entry = get_task_handler(task_id=tid, queue_dir=str(tmp_path))["history"][-1]
    assert entry["actor"] == "operator"
    assert entry["action"] == "requeue"


def test_requeue_requires_the_secret(env, client):
    _, tmp_path = env
    r = _seed_result(tmp_path)
    tid = _dead_letter(tmp_path, r)

    resp = client.post(f"/tasks/{tid}/requeue", json={"note": "n"})

    assert resp.status_code == 401
    assert not (tmp_path / r["filename"]).exists()
    assert (tmp_path / "dead-letters" / r["filename"]).is_file()


def test_requeue_wrong_secret_is_rejected(env, client):
    _, tmp_path = env
    r = _seed_result(tmp_path)
    tid = _dead_letter(tmp_path, r)

    resp = client.post(f"/tasks/{tid}/requeue", headers={"X-Task-Queue-Secret": "wrong"}, json={})

    assert resp.status_code == 401
    assert (tmp_path / "dead-letters" / r["filename"]).is_file()


def test_requeue_of_a_live_task_is_404(env, client):
    """Terminal immutability is untouched: only dead-letters/ is reachable from here."""
    _, tmp_path = env
    tid = _seed(tmp_path, status="failed")

    resp = client.post(f"/tasks/{tid}/requeue", headers=AUTH, json={})

    assert resp.status_code == 404
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "failed"


def test_requeue_tolerates_an_empty_body(env, client):
    _, tmp_path = env
    tid = _dead_letter(tmp_path, _seed_result(tmp_path))

    resp = client.post(f"/tasks/{tid}/requeue", headers=AUTH)

    assert resp.status_code == 200


# ── error surfacing ────────────────────────────────────────────────────


def test_approve_not_found_404(env, client):
    import uuid

    resp = client.post(f"/tasks/{uuid.uuid4()}/approve", headers=AUTH, json={"actor": "ted"})
    assert resp.status_code == 404


def test_status_invalid_transition_400(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path, status="in-progress")
    # approved is only reachable from submitted/pending-approval — rejected from in-progress
    resp = client.post(
        f"/tasks/{tid}/status",
        headers=AUTH,
        json={"status": "approved", "actor": "ted"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_status_empty_body_rejected(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    # No status in body -> handler rejects invalid status -> 400
    resp = client.post(f"/tasks/{tid}/status", headers=AUTH)
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Operator sweep — POST /tasks/{id}/update
# --------------------------------------------------------------------------- #


def test_sweep_requires_the_shared_secret(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path, status="in-progress")
    resp = client.post(f"/tasks/{tid}/update", json={"status": "completed"})

    assert resp.status_code == 401
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "in-progress"


def test_sweep_closes_another_agents_task_and_records_both_names(env, client):
    """
    The replacement for the route this build closed. An agent used to tidy up a stranded
    task by passing the other agent's name as `actor`; now the operator does it explicitly
    and the history says so — `actor: operator` alongside `on_behalf_of: developer`, rather
    than a record that reads as though developer closed its own work.
    """
    _, tmp_path = env
    tid = _seed(tmp_path, status="in-progress")

    resp = client.post(
        f"/tasks/{tid}/update",
        headers=AUTH,
        json={
            "status": "completed",
            "on_behalf_of": "developer",
            "note": "stranded; swept during queue cleanup",
        },
    )

    assert resp.status_code == 200
    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["status"] == "completed"

    entry = task["history"][-1]
    assert entry["actor"] == "operator"
    assert entry["on_behalf_of"] == "developer"


def test_sweep_rejects_a_wrong_on_behalf_of(env, client):
    """
    Naming the wrong agent means the operator is closing a task they have misidentified.
    Recording that as a deliberate sweep would put a confident falsehood in the audit trail.
    """
    _, tmp_path = env
    tid = _seed(tmp_path, status="in-progress")

    resp = client.post(
        f"/tasks/{tid}/update",
        headers=AUTH,
        json={"status": "completed", "on_behalf_of": "security"},
    )

    assert resp.status_code == 400
    assert "is not the target agent" in resp.json()["error"]
    assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "in-progress"


def test_sweep_without_on_behalf_of_is_a_plain_operator_close(env, client):
    """on_behalf_of is optional — omitting it is the operator acting in its own name."""
    _, tmp_path = env
    tid = _seed(tmp_path, status="in-progress")

    resp = client.post(f"/tasks/{tid}/update", headers=AUTH, json={"status": "completed"})

    assert resp.status_code == 200
    entry = get_task_handler(task_id=tid, queue_dir=str(tmp_path))["history"][-1]
    assert entry["actor"] == "operator"
    assert "on_behalf_of" not in entry


def test_on_behalf_of_is_refused_for_a_non_operator_actor():
    """
    Handler-level guard, independent of the route that pins the actor. If a future caller
    reaches update_task_handler directly, acting-for must still be operator-only.
    """
    r = update_task_handler(
        task_id="00000000-0000-0000-0000-000000000000",
        status="completed",
        actor="writer",
        on_behalf_of="developer",
    )

    assert r["ok"] is False
    assert "reserved for the 'operator' actor" in r["error"]
