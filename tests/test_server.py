"""
Wiring tests for src/server.py — exercise each @mcp.tool() entrypoint so the thin
delegation layer is covered (it was previously 0%). FastMCP 3.x returns the original
function from @mcp.tool(), so the tools are directly callable. We point the module's
QUEUE_DIR at a tmp dir via monkeypatch.
"""

import importlib

import pytest
import yaml


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Import src.server with QUEUE_DIR pointed at an isolated tmp queue."""
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    return srv


def _set_status_on_disk(tmp_path, filename, status):
    path = tmp_path / filename
    with open(path) as f:
        data = yaml.safe_load(f)
    data["status"] = status
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _submit(server):
    return server.submit_task(
        source_agent="research",
        target_agent="developer",
        task_type="build",
        summary="s",
        description="d",
    )


def test_server_submit_and_get(server):
    r = _submit(server)
    assert r["ok"] is True
    task = server.get_task(r["task_id"])
    assert task["id"] == r["task_id"]
    assert task["status"] == "submitted"


def test_server_list_tasks(server):
    _submit(server)
    listed = server.list_tasks(target_agent="developer")
    assert len(listed) == 1
    assert listed[0]["target_agent"] == "developer"


def test_server_update_task(server, tmp_path):
    r = _submit(server)
    _set_status_on_disk(tmp_path, r["filename"], "approved")
    upd = server.update_task(task_id=r["task_id"], status="in-progress", actor="developer")
    assert upd["ok"] is True
    assert server.get_task(r["task_id"])["status"] == "in-progress"


def test_server_set_task_status_approve(server):
    r = _submit(server)
    out = server.set_task_status(task_id=r["task_id"], status="approved", actor="ted")
    assert out["ok"] is True
    assert server.get_task(r["task_id"])["status"] == "approved"


def test_server_set_task_status_override(server, tmp_path):
    r = _submit(server)
    _set_status_on_disk(tmp_path, r["filename"], "approved")
    out = server.set_task_status(
        task_id=r["task_id"],
        status="in-progress",
        actor="ted",
        note="advance",
        allow_override=True,
    )
    assert out["ok"] is True


def test_server_cancel_task(server):
    r = _submit(server)
    out = server.cancel_task(task_id=r["task_id"], actor="ted", note="stale")
    assert out["ok"] is True
    assert server.get_task(r["task_id"])["status"] == "cancelled"


def test_server_park_and_unpark(server, tmp_path):
    r = _submit(server)
    _set_status_on_disk(tmp_path, r["filename"], "approved")

    p = server.park_task(task_id=r["task_id"], actor="ted")
    assert p["ok"] is True
    # Stays visible — the point of park-as-status.
    assert len(server.list_tasks()) == 1
    assert server.get_task(r["task_id"])["status"] == "parked"

    u = server.unpark_task(task_id=r["task_id"], actor="ted")
    assert u["ok"] is True
    assert server.get_task(r["task_id"])["status"] == "approved"


def test_server_unpark_explicit_status(server):
    r = _submit(server)
    server.park_task(task_id=r["task_id"], actor="ted")
    u = server.unpark_task(task_id=r["task_id"], actor="ted", status="approved")
    assert u["ok"] is True
    assert server.get_task(r["task_id"])["status"] == "approved"


def test_server_amend_task(server):
    r = _submit(server)
    out = server.amend_task(
        task_id=r["task_id"],
        amendment="preflight answered the open question",
        actor="research",
        reason="post-queue correction",
    )
    assert out["ok"] is True
    assert out["amendment_count"] == 1

    task = server.get_task(r["task_id"])
    assert task["payload"]["description"] == "d"
    assert task["payload"]["amendments"][0]["text"] == "preflight answered the open question"


def test_server_amend_task_target_agent_rejected(server):
    r = _submit(server)
    out = server.amend_task(task_id=r["task_id"], amendment="nope", actor="developer")
    assert out["ok"] is False
