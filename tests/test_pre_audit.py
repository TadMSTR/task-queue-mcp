"""
`pre_audit`: a build's fixer task (audit-fixer-2026-11 p2).

The dispatcher launches a fresh session of the target agent on a handoff file whose path it
builds from build_name, audit_round and handoff_seq, so every one of those is required and
bounded here, and the task must be addressed by an agent to itself. Each refusal is tested
alone, against a submission that is otherwise valid.
"""

import importlib

import pytest
import yaml

import src.auth as auth_mod
from src.tools.queue import FIXER_AGENTS, VALID_TASK_TYPES, submit_task_handler

VALID = dict(
    source_agent="developer",
    target_agent="developer",
    task_type="pre_audit",
    summary="Pre-audit fixer: b-1 round 1 h0",
    description="d",
    build_name="b-1",
    audit_round=1,
    handoff_seq=0,
)


def _submit(tmp_path, **kw):
    args = dict(VALID, queue_dir=str(tmp_path))
    args.update(kw)
    return submit_task_handler(**args)


def _stored(tmp_path, r):
    return yaml.safe_load((tmp_path / r["filename"]).read_text())


def test_pre_audit_is_a_valid_task_type():
    assert "pre_audit" in VALID_TASK_TYPES


def test_fixer_agents_are_the_build_agents():
    assert FIXER_AGENTS == {"developer", "sysadmin", "fleet"}


@pytest.mark.parametrize("agent", sorted(FIXER_AGENTS))
def test_a_valid_pre_audit_is_accepted_and_stored(tmp_path, agent):
    r = _submit(tmp_path, source_agent=agent, target_agent=agent, audit_round=20, handoff_seq=2)
    assert r["ok"], r
    t = _stored(tmp_path, r)
    assert t["task_type"] == "pre_audit"
    assert (t["build_name"], t["audit_round"], t["handoff_seq"]) == ("b-1", 20, 2)
    assert t["status"] == "submitted" and t["requires_approval"] is False
    assert "originating_task_id" not in t["payload"]


def test_workflow_mode_is_kept_as_passed(tmp_path):
    r = _submit(tmp_path, workflow_mode="auto")
    assert _stored(tmp_path, r)["workflow_mode"] == "auto"


# --- each rule refused alone ------------------------------------------------------------


def test_refused_when_source_is_not_target(tmp_path):
    r = _submit(tmp_path, source_agent="developer", target_agent="sysadmin")
    assert not r["ok"] and "addressed to its submitter" in r["error"]


@pytest.mark.parametrize("agent", ["security", "research", "writer", "steward", "operator"])
def test_refused_for_a_non_build_agent(tmp_path, agent):
    r = _submit(tmp_path, source_agent=agent, target_agent=agent)
    assert not r["ok"] and "build agent" in r["error"]


def test_refused_without_build_name(tmp_path):
    r = _submit(tmp_path, build_name=None)
    assert not r["ok"] and "requires build_name" in r["error"]


@pytest.mark.parametrize("name", ["", "a.b", "a/b", "../x", "x" * 129])
def test_refused_with_a_bad_build_name(tmp_path, name):
    r = _submit(tmp_path, build_name=name)
    assert not r["ok"] and "build_name" in r["error"]


def test_refused_without_audit_round(tmp_path):
    r = _submit(tmp_path, audit_round=None)
    assert not r["ok"] and "requires audit_round" in r["error"]


@pytest.mark.parametrize("rnd", [0, 21, -1, True, "1", 1.0])
def test_refused_with_a_bad_audit_round(tmp_path, rnd):
    r = _submit(tmp_path, audit_round=rnd)
    assert not r["ok"] and "audit_round" in r["error"]


def test_refused_without_handoff_seq(tmp_path):
    r = _submit(tmp_path, handoff_seq=None)
    assert not r["ok"] and "requires handoff_seq" in r["error"]


@pytest.mark.parametrize("seq", [-1, 3, True, False, "0", 0.0])
def test_refused_with_a_bad_handoff_seq(tmp_path, seq):
    r = _submit(tmp_path, handoff_seq=seq)
    assert not r["ok"] and "handoff_seq" in r["error"]


def test_refused_with_originating_task_id(tmp_path):
    r = _submit(tmp_path, originating_task_id="9e92ac98-fd7a-4de5-abfd-59d7d45bd03a")
    assert not r["ok"] and "originating_task_id" in r["error"]


def test_refused_with_requires_approval(tmp_path):
    r = _submit(tmp_path, requires_approval=True)
    assert not r["ok"] and "requires_approval" in r["error"]


def test_nothing_is_written_on_a_refusal(tmp_path):
    _submit(tmp_path, source_agent="developer", target_agent="security")
    assert not list(tmp_path.glob("*.yml"))


# --- handoff_seq off a pre_audit ---------------------------------------------------------


@pytest.mark.parametrize("task_type", ["build", "audit", "fix", "notify"])
def test_handoff_seq_is_refused_on_other_types(tmp_path, task_type):
    r = submit_task_handler(
        "developer", "developer", task_type, "s", "d", queue_dir=str(tmp_path), handoff_seq=0
    )
    assert not r["ok"] and "handoff_seq is only valid" in r["error"]


def test_other_types_are_unchanged_without_handoff_seq(tmp_path):
    r = submit_task_handler("research", "developer", "build", "s", "d", queue_dir=str(tmp_path))
    assert r["ok"]
    assert "handoff_seq" not in _stored(tmp_path, r)


# --- through the tool, with identity binding ---------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    return srv


def _tool_submit(server, **kw):
    args = {k: v for k, v in VALID.items()}
    args.update(kw)
    return server.submit_task(**args)


def test_tool_accepts_an_agents_own_fixer(server, monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "developer")
    r = _tool_submit(server)
    assert r["ok"], r
    assert _stored(tmp_path, r)["handoff_seq"] == 0


def test_tool_refuses_a_fixer_filed_as_another_agent(server, monkeypatch, tmp_path):
    """research claims to be developer queueing developer's fixer: bind_actor refuses it."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "research")
    r = _tool_submit(server)
    assert r["ok"] is False
    assert "does not match the authenticated identity" in r["error"]
    assert not list(tmp_path.glob("*.yml"))


def test_tool_refuses_a_fixer_for_another_agent_under_own_identity(server, monkeypatch, tmp_path):
    """Honest identity, someone else's fixer: the source == target rule refuses it."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "sysadmin")
    r = _tool_submit(server, source_agent="sysadmin", target_agent="developer")
    assert r["ok"] is False and "addressed to its submitter" in r["error"]
