"""
Tests for per-agent bearer token auth on the MCP tool path (vikunja#387).

The regression test that matters is test_unauthenticated_mcp_request_is_refused: before
v0.7.0 an unauthenticated POST to /mcp returned 200 with a session id, which is what made
every ownership rule in this server advisory. Everything else here guards the ways the
token configuration can fail open or silently mis-attribute an agent's actions.
"""

import asyncio
import importlib

import pytest
from starlette.testclient import TestClient

import src.auth as auth_mod
from src.auth import (
    MIN_TOKEN_LENGTH,
    AuthConfigError,
    build_verifier,
    load_agent_tokens,
)

GOOD = "x" * MIN_TOKEN_LENGTH
OTHER = "y" * MIN_TOKEN_LENGTH


# --------------------------------------------------------------------------- #
# load_agent_tokens
# --------------------------------------------------------------------------- #


def test_no_tokens_configured_returns_empty():
    assert load_agent_tokens(env={"UNRELATED": "value"}) == {}


def test_token_env_var_maps_to_agent_identity():
    assert load_agent_tokens(env={"TASK_QUEUE_TOKEN_DEVELOPER": GOOD}) == {GOOD: "developer"}


def test_underscore_in_env_suffix_becomes_hyphen():
    """DOC_HEALTH -> doc-health. Forge agent names are hyphenated, so this round-trips."""
    assert load_agent_tokens(env={"TASK_QUEUE_TOKEN_DOC_HEALTH": GOOD}) == {GOOD: "doc-health"}


def test_multiple_agents_each_get_their_own_identity():
    tokens = load_agent_tokens(
        env={"TASK_QUEUE_TOKEN_DEVELOPER": GOOD, "TASK_QUEUE_TOKEN_SECURITY": OTHER}
    )
    assert tokens == {GOOD: "developer", OTHER: "security"}


def test_bare_prefix_is_ignored():
    """TASK_QUEUE_TOKEN_ with no suffix would resolve to an empty identity."""
    assert load_agent_tokens(env={"TASK_QUEUE_TOKEN_": GOOD}) == {}


def test_empty_token_value_is_fatal():
    """A var that failed to interpolate must not leave that agent silently unauthenticated."""
    with pytest.raises(AuthConfigError, match="set but empty"):
        load_agent_tokens(env={"TASK_QUEUE_TOKEN_DEVELOPER": "   "})


def test_short_token_is_fatal():
    with pytest.raises(AuthConfigError, match="too short"):
        load_agent_tokens(env={"TASK_QUEUE_TOKEN_DEVELOPER": "short"})


def test_token_for_reserved_operator_identity_is_fatal():
    """
    `operator` is exempt from every ownership check. A token minting it on the agent-facing
    transport would hand its holder the whole queue.
    """
    with pytest.raises(AuthConfigError, match="reserved identity"):
        load_agent_tokens(env={"TASK_QUEUE_TOKEN_OPERATOR": GOOD})


def test_two_agents_sharing_a_token_is_fatal():
    """
    Sharing collapses in the token->identity dict and attributes both agents' actions to
    one of them. Attribution is the point of the whole scheme, so this must not start.
    """
    with pytest.raises(AuthConfigError, match="reuses the token"):
        load_agent_tokens(
            env={"TASK_QUEUE_TOKEN_DEVELOPER": GOOD, "TASK_QUEUE_TOKEN_SECURITY": GOOD}
        )


# --------------------------------------------------------------------------- #
# build_verifier
# --------------------------------------------------------------------------- #


def test_build_verifier_returns_none_when_unconfigured():
    """None is what FastMCP(auth=...) takes to mean 'no auth' — stdio and unit tests."""
    assert build_verifier({}) is None


def test_verifier_accepts_a_known_token_and_carries_the_identity():
    verifier = build_verifier({GOOD: "developer"})
    # Driven with asyncio.run rather than an async test — this repo has no async pytest
    # plugin and one verify_token call does not justify adding a dependency.
    token = asyncio.run(verifier.verify_token(GOOD))

    assert token is not None
    # StaticTokenVerifier does not populate .subject — identity rides in the claims.
    assert token.claims["sub"] == "developer"
    assert token.client_id == "developer"


def test_verifier_rejects_an_unknown_token():
    verifier = build_verifier({GOOD: "developer"})
    assert asyncio.run(verifier.verify_token("not-a-real-token")) is None


# --------------------------------------------------------------------------- #
# End-to-end over HTTP — the vikunja#387 regression
# --------------------------------------------------------------------------- #


@pytest.fixture
def authed_server(monkeypatch, tmp_path):
    """Reload src.server with one agent token set, so auth is active at import time."""
    monkeypatch.setenv("TASK_QUEUE_TOKEN_DEVELOPER", GOOD)
    import src.server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "QUEUE_DIR", str(tmp_path))
    yield srv
    # Restore the unauthenticated module state for any test importing it afterwards.
    monkeypatch.delenv("TASK_QUEUE_TOKEN_DEVELOPER", raising=False)
    importlib.reload(srv)


def _initialize_body():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def test_token_is_loaded_into_the_server_module(authed_server):
    assert authed_server._agent_tokens == {GOOD: "developer"}


def test_unauthenticated_mcp_request_is_refused(authed_server):
    """
    vikunja#387 regression. This exact request returned HTTP 200 with an mcp-session-id
    before v0.7.0, from the published port and from any container on the shared network.
    """
    with TestClient(authed_server.mcp.http_app()) as client:
        resp = client.post("/mcp", json=_initialize_body(), headers=HEADERS)

    assert resp.status_code == 401
    assert "mcp-session-id" not in resp.headers


def test_wrong_bearer_token_is_refused(authed_server):
    with TestClient(authed_server.mcp.http_app()) as client:
        resp = client.post(
            "/mcp",
            json=_initialize_body(),
            headers={**HEADERS, "Authorization": f"Bearer {OTHER}"},
        )

    assert resp.status_code == 401


def test_correct_bearer_token_is_accepted(authed_server):
    """The other half of the cutover: a caller that does present its token still works."""
    with TestClient(authed_server.mcp.http_app()) as client:
        resp = client.post(
            "/mcp",
            json=_initialize_body(),
            headers={**HEADERS, "Authorization": f"Bearer {GOOD}"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("mcp-session-id")


def test_control_routes_stay_on_the_shared_secret(authed_server, monkeypatch):
    """
    The control routes are custom_route handlers and sit outside the transport's auth
    provider by design — they are the operator surface. Enabling MCP auth must neither
    open them nor start demanding a bearer token from the CloudCLI plugin or Matrix bot.
    """
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", "control-secret")

    with TestClient(authed_server.mcp.http_app()) as client:
        # No secret, no bearer -> still 401 from the shared-secret gate, not a 200.
        assert client.get("/queue/summary").status_code == 401
        # Correct secret, no bearer -> still works.
        resp = client.get("/queue/summary", headers={"X-Task-Queue-Secret": "control-secret"})

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --------------------------------------------------------------------------- #
# resolve_identity — the real path the adversarial suite stubs out
# --------------------------------------------------------------------------- #


class _Token:
    def __init__(self, claims, client_id="cid"):
        self.claims = claims
        self.client_id = client_id


def test_resolve_identity_is_none_without_a_token(monkeypatch):
    """stdio, unit tests, and the control routes all land here — and None is not operator."""
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: None)
    assert auth_mod.resolve_identity() is None


def test_resolve_identity_reads_the_sub_claim(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: _Token({"sub": "developer"}))
    assert auth_mod.resolve_identity() == "developer"


def test_resolve_identity_falls_back_to_client_id(monkeypatch):
    """StaticTokenVerifier populates client_id but never .subject; sub is the primary."""
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: _Token({}, client_id="writer"))
    assert auth_mod.resolve_identity() == "writer"


def test_resolve_identity_treats_an_empty_identity_as_absent(monkeypatch):
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: _Token({"sub": ""}, client_id=""))
    assert auth_mod.resolve_identity() is None


def test_bind_actor_accepts_a_matching_claim(monkeypatch):
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "developer")
    assert auth_mod.bind_actor("developer") == (True, "developer")


def test_bind_actor_derives_the_actor_when_none_is_claimed(monkeypatch):
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: "developer")
    assert auth_mod.bind_actor(None) == (True, "developer")


def test_bind_actor_requires_an_actor_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: None)
    ok, err = auth_mod.bind_actor("")
    assert ok is False
    assert "actor is required" in err


def test_require_operator_surface_permits_the_call_when_unauthenticated(monkeypatch):
    """The control routes reach the same handlers and must not be refused by this guard."""
    monkeypatch.setattr(auth_mod, "resolve_identity", lambda: None)
    assert auth_mod.require_operator_surface("cancel_task") is None


def test_operator_identity_is_spelled_exactly_once_in_the_source():
    """
    Regression for the 2026-08-16 audit LOW. The operator string was spelled independently
    in three places — queue.OPERATOR_ACTOR, server.OPERATOR_ACTOR, and auth's
    RESERVED_IDENTITIES — and nothing tied them together. Drift would raise neither an
    import error nor a type error, and fails silently in both directions: strip the HTTP
    control routes of the exemption every ownership check grants them, or let a token be
    minted for the very identity those checks exempt.

    THIS IS A SOURCE-LEVEL CHECK ON PURPOSE. The obvious runtime version —
    `server.OPERATOR_ACTOR is queue.OPERATOR_ACTOR` — is vacuous: CPython interns
    identifier-like literals, so two separately-compiled `= "operator"` assignments in
    different modules are the *same object* and `is` passes. Verified, not assumed. A test
    that claims to catch drift while passing unconditionally is worse than no test, so the
    check reads the files instead.
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src"
    assignment = re.compile(r"""^\s*\w+\s*=\s*["']operator["']""", re.MULTILINE)

    sites = {
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if assignment.search(path.read_text())
    }

    assert sites == {"tools/queue.py"}, (
        f"the operator identity must be defined once, in tools/queue.py; found in {sorted(sites)}. "
        "Import OPERATOR_ACTOR from there instead of re-spelling the literal."
    )
    # ...and the importers really do resolve to it.
    import src.server as srv
    from src.tools.queue import OPERATOR_ACTOR as queue_operator

    assert srv.OPERATOR_ACTOR == queue_operator
    assert auth_mod.RESERVED_IDENTITIES == frozenset({queue_operator})
