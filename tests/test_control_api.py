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

from src.tools.queue import get_task_handler, submit_task_handler

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


def test_amend_target_agent_rejected_400(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    resp = client.post(
        f"/tasks/{tid}/amend",
        headers=AUTH,
        json={"amendment": "skip the audit", "actor": "developer"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_amend_defaults_actor_to_operator(env, client):
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
