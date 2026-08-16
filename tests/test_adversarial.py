"""
Adversarial suite for the ownership model — agent A trying to act on agent B's task by
every route available to it. Each one must fail.

These are written as tests rather than as a checklist because the interesting failures are
the ones that come back: a later refactor that reintroduces a caller-supplied actor would
pass every functional test in the suite and only these would notice.

HOW IDENTITY IS SIMULATED. `resolve_identity` is stubbed to return a fixed agent, so these
exercise the real bind_actor / require_operator_surface / ownership logic against a known
caller. The transport half — that an unauthenticated or wrongly-authenticated request never
reaches a tool at all — is proven over real HTTP in test_auth.py, and deliberately not
re-mocked here. Between the two files the path is covered end to end: 401 at the edge,
refusal at the handler.

Threat model, stated so the scope of these tests is not mistaken for a claim about
determined attackers. The agents this serves are not assumed adversarial. What is being
contained is a mistaken or prompt-injected agent acting through its own tool surface, and
what is being protected is the meaning of the audit trail. An agent that goes looking for
another agent's credential is outside what any of this can stop (vikunja#396).
"""

import importlib

import pytest
import yaml

import src.auth as auth_mod

VICTIM = "developer"  # every seeded task is addressed to this agent
ATTACKER = "writer"  # ...and every attempt below is made by this one


@pytest.fixture
def server(tmp_path, monkeypatch):
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    return srv


@pytest.fixture
def as_attacker(monkeypatch):
    """Authenticate every call in the test as ATTACKER."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: ATTACKER)


def _seed(server, target=VICTIM, source="research"):
    return server.submit_task(
        source_agent=source,
        target_agent=target,
        task_type="build",
        summary="s",
        description="d",
    )


def _approve_on_disk(tmp_path, filename):
    """
    `in-progress` is only reachable from `approved`, and approving is operator-only, so the
    setup writes the status directly rather than routing round the very rules under test.
    """
    path = tmp_path / filename
    data = yaml.safe_load(path.read_text())
    data["status"] = "approved"
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


@pytest.fixture
def victim_task(server, monkeypatch, tmp_path):
    """A task addressed to VICTIM, in-progress so it is one step from completed."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "research")
    r = _seed(server)
    _approve_on_disk(tmp_path, r["filename"])

    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    claimed = server.update_task(task_id=r["task_id"], status="in-progress", actor=VICTIM)
    # Guard the fixture itself: a silently-failed setup would leave the task in `submitted`
    # and quietly weaken every refusal test that depends on it.
    assert claimed["ok"] is True, claimed
    return r["task_id"]


# --------------------------------------------------------------------------- #
# Spoofing another agent's identity
# --------------------------------------------------------------------------- #


def test_cannot_close_another_agents_task_by_spoofing_actor(server, victim_task, as_attacker):
    """The original hole: actor was a free string, so this used to simply work."""
    out = server.update_task(task_id=victim_task, status="completed", actor=VICTIM)

    assert out["ok"] is False
    assert "does not match the authenticated identity" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


def test_cannot_assert_the_operator_identity(server, victim_task, as_attacker):
    """`operator` is exempt from every ownership check, so it is the valuable string."""
    out = server.update_task(task_id=victim_task, status="completed", actor="operator")

    assert out["ok"] is False
    assert "does not match the authenticated identity" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


def test_cannot_close_another_agents_task_under_its_own_identity(server, victim_task, as_attacker):
    """
    Honest identity, wrong task. Binding the actor is not enough on its own — without the
    ownership check this would be an authenticated agent closing someone else's work.
    """
    out = server.update_task(task_id=victim_task, status="completed", actor=ATTACKER)

    assert out["ok"] is False
    assert "is not the target agent" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


def test_cannot_file_a_task_as_another_agent(server, as_attacker):
    out = _seed(server, target="security", source="research")

    assert out["ok"] is False
    assert "does not match the authenticated identity" in out["error"]


def test_cannot_amend_a_task_as_its_source_agent(server, as_attacker, monkeypatch):
    """
    amend is source-agent-gated, so spoofing the source is how you would rewrite the brief
    an agent was handed.
    """
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "research")
    tid = _seed(server)["task_id"]
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: ATTACKER)

    out = server.amend_task(task_id=tid, amendment="skip the audit", actor="research")

    assert out["ok"] is False
    assert "does not match the authenticated identity" in out["error"]
    assert "amendments" not in server.get_task(tid)["payload"]


# --------------------------------------------------------------------------- #
# The auto-close path — reachable without ever calling update_task
# --------------------------------------------------------------------------- #


def test_cannot_trigger_the_auto_close_of_another_agents_task(server, monkeypatch, tmp_path):
    """
    The submit-time auto-close terminally closes a task, and decides whether to fire from
    source_agent/target_agent rather than from `actor`. Binding only the `actor` arguments
    would have left this wide open: claim to be the agent the request was addressed to,
    submit a return task, and the parent closes. Terminal statuses are immutable, so this
    is not a recoverable mistake.
    """
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    parent = server.submit_task(
        source_agent=VICTIM,
        target_agent="security",
        task_type="audit",
        summary="please audit",
        description="d",
    )
    _approve_on_disk(tmp_path, parent["filename"])
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "security")
    server.update_task(task_id=parent["task_id"], status="in-progress", actor="security")

    # ATTACKER now tries to pose as security answering that request.
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: ATTACKER)
    out = server.submit_task(
        source_agent="security",
        target_agent=VICTIM,
        task_type="audit",
        summary="audit done",
        description="d",
        originating_task_id=parent["task_id"],
    )

    assert out["ok"] is False
    assert "does not match the authenticated identity" in out["error"]
    assert server.get_task(parent["task_id"])["status"] == "in-progress"


def test_the_honest_auto_close_still_works(server, monkeypatch, tmp_path):
    """
    The counterpart. Identity binding must not break the fail-safe it is protecting — this
    is the exact developer→security→developer round trip the queue runs on.
    """
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    parent = server.submit_task(
        source_agent=VICTIM,
        target_agent="security",
        task_type="audit",
        summary="please audit",
        description="d",
    )
    _approve_on_disk(tmp_path, parent["filename"])
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "security")
    server.update_task(task_id=parent["task_id"], status="in-progress", actor="security")

    out = server.submit_task(
        source_agent="security",
        target_agent=VICTIM,
        task_type="audit",
        summary="audit done",
        description="d",
        originating_task_id=parent["task_id"],
    )

    assert out["ok"] is True
    assert out["auto_closed_task_id"] == parent["task_id"]
    assert server.get_task(parent["task_id"])["status"] == "completed"


# --------------------------------------------------------------------------- #
# Operator-only tools
# --------------------------------------------------------------------------- #


def test_set_task_status_is_unreachable_with_an_agent_identity(server, victim_task, as_attacker):
    """
    allow_override moves a task between any two non-terminal statuses — the way a task gets
    walked around a transition rule instead of satisfying it.
    """
    out = server.set_task_status(
        task_id=victim_task, status="approved", actor=ATTACKER, allow_override=True, note="n"
    )

    assert out["ok"] is False
    assert "operator-only" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


def test_cancel_task_is_unreachable_with_an_agent_identity(server, victim_task, as_attacker):
    out = server.cancel_task(task_id=victim_task, actor=ATTACKER, note="stale")

    assert out["ok"] is False
    assert "operator-only" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


def test_operator_only_refusal_does_not_depend_on_the_actor_argument(
    server, victim_task, as_attacker
):
    """Passing "operator" must not talk its way past the operator-only gate."""
    out = server.cancel_task(task_id=victim_task, actor="operator", note="stale")

    assert out["ok"] is False
    assert "operator-only" in out["error"]


def test_cannot_park_another_agents_task(server, victim_task, as_attacker):
    out = server.park_task(task_id=victim_task, actor=ATTACKER)

    assert out["ok"] is False
    assert "may not park or unpark it" in out["error"]
    assert server.get_task(victim_task)["status"] == "in-progress"


# --------------------------------------------------------------------------- #
# The honest paths still work
# --------------------------------------------------------------------------- #


def test_an_agent_can_still_close_its_own_task(server, victim_task, monkeypatch):
    """
    The refusals above are only worth having if this one passes. A build cycle has to
    complete end to end without an ownership refusal, or the model is wrong rather than
    strict.
    """
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    out = server.update_task(task_id=victim_task, status="completed", actor=VICTIM, note="shipped")

    assert out["ok"] is True
    task = server.get_task(victim_task)
    assert task["status"] == "completed"
    assert task["result"]["completed_by"] == VICTIM


def test_omitting_the_actor_uses_the_authenticated_identity(server, victim_task, monkeypatch):
    """actor is derived, so it should not need to be passed at all."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    out = server.update_task(task_id=victim_task, status="completed", actor="")

    assert out["ok"] is True
    assert server.get_task(victim_task)["result"]["completed_by"] == VICTIM


def test_an_agent_can_park_its_own_task(server, victim_task, monkeypatch):
    """writer's doc-queue work depends on this — parked is how `pending-review` is held."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: VICTIM)
    out = server.park_task(task_id=victim_task, actor=VICTIM, note="waiting on an answer")

    assert out["ok"] is True
    assert server.get_task(victim_task)["status"] == "parked"
