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

from src.tools.queue import submit_task_handler, get_task_handler

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
        source_agent="research", target_agent="developer", task_type="build",
        summary="s", description="d", queue_dir=str(tmp_path),
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


def test_quarantine_then_restore_ok(env, client):
    _, tmp_path = env
    tid = _seed(tmp_path)
    q = client.post(f"/tasks/{tid}/quarantine", headers=AUTH, json={"actor": "ted"})
    assert q.status_code == 200
    assert (tmp_path / "quarantine").exists()

    r = client.post(f"/tasks/{tid}/restore", headers=AUTH, json={"actor": "ted"})
    assert r.status_code == 200


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
