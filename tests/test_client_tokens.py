"""
Scoped client tokens and the read API (v0.11.0, operator-panel-2026-09 part 2).

The custom routes used to sit behind one shared secret that could both read and write.
Each client now has its own token, stored server-side as a sha256 digest, with explicit
`read` / `operator-write` scopes. These tests cover:

  - every configuration load_client_tokens refuses to start with
  - the scope gate on every custom route, with the routes enumerated from the app so a
    route added later cannot skip it
  - that an agent bearer, which FastMCP's app-wide middleware does authenticate, grants
    nothing on these routes, and that a client token grants nothing on /mcp
  - GET /tasks reporting `count` and `truncated` honestly
  - `channel` on the history entry of every write route
  - the retired shared secret granting nothing, even when the variable is set again

No assertion here compares a token map or prints a token. The tokens are test constants,
but the habit is what #568 is about.
"""

import importlib
import logging
import uuid

import pytest
import yaml
from starlette.routing import Route
from starlette.testclient import TestClient

from src.auth import (
    CLIENT_TOKEN_HEADER,
    RETIRED_CHANNEL,
    RETIRED_SECRET_HEADER,
    SCOPE_OPERATOR_WRITE,
    SCOPE_READ,
    AuthConfigError,
    authorize_client,
    load_client_tokens,
    retired_secret_set,
    token_digest,
)
from src.tools.queue import (
    LIST_PAGE_MAX,
    LIST_TASKS_MAX,
    get_task_handler,
    list_tasks_handler,
    submit_task_handler,
)

AGENT_TOKEN = "agent-token-" + "a" * 32
READ_TOKEN = "read-token-" + "r" * 32
WRITE_TOKEN = "write-token-" + "w" * 32
BOTH_TOKEN = "both-token-" + "b" * 32
RETIRED_SECRET = "retired-shared-secret-value"

CLIENT_ENV = {
    "TASK_QUEUE_CLIENT_READER": token_digest(READ_TOKEN),
    "TASK_QUEUE_CLIENT_SCOPES_READER": "read",
    "TASK_QUEUE_CLIENT_WRITER_ONLY": token_digest(WRITE_TOKEN),
    "TASK_QUEUE_CLIENT_SCOPES_WRITER_ONLY": "operator-write",
    "TASK_QUEUE_CLIENT_MATRIX_BOT": token_digest(BOTH_TOKEN),
    "TASK_QUEUE_CLIENT_SCOPES_MATRIX_BOT": "read,operator-write",
}


def _channels(clients) -> list[str]:
    return sorted(c.channel for c in clients.values())


# --------------------------------------------------------------------------- #
# load_client_tokens
# --------------------------------------------------------------------------- #


def test_zero_clients_is_allowed():
    assert load_client_tokens(env={"UNRELATED": "x"}) == {}


def test_clients_load_with_their_channels_and_scopes():
    clients = load_client_tokens(env=CLIENT_ENV)
    assert _channels(clients) == ["matrix-bot", "reader", "writer-only"]
    by_channel = {c.channel: c.scopes for c in clients.values()}
    assert by_channel["reader"] == {SCOPE_READ}
    assert by_channel["writer-only"] == {SCOPE_OPERATOR_WRITE}
    assert by_channel["matrix-bot"] == {SCOPE_READ, SCOPE_OPERATOR_WRITE}


def test_the_map_is_keyed_by_digest_never_by_token():
    clients = load_client_tokens(env=CLIENT_ENV)
    assert READ_TOKEN not in clients
    assert token_digest(READ_TOKEN) in clients


@pytest.mark.parametrize(
    "bad",
    [
        READ_TOKEN,  # the likeliest mistake: plaintext pasted where the digest goes
        token_digest(READ_TOKEN)[len("sha256:") :],  # no prefix
        token_digest(READ_TOKEN).upper().replace("SHA256:", "sha256:"),  # uppercase hex
        token_digest(READ_TOKEN)[:-1],  # 63 hex
        token_digest(READ_TOKEN) + "0",  # 65 hex
        "sha256:" + "g" * 64,  # not hex
        "",
    ],
)
def test_a_malformed_digest_refuses_to_start(bad):
    env = {"TASK_QUEUE_CLIENT_READER": bad, "TASK_QUEUE_CLIENT_SCOPES_READER": "read"}
    with pytest.raises(AuthConfigError) as exc:
        load_client_tokens(env=env)
    # The refusal must not echo the value: it may be a plaintext token.
    assert READ_TOKEN not in str(exc.value)
    if bad:
        assert bad not in str(exc.value)


def test_a_digest_shared_by_two_clients_refuses_to_start():
    env = {
        "TASK_QUEUE_CLIENT_ONE": token_digest(READ_TOKEN),
        "TASK_QUEUE_CLIENT_SCOPES_ONE": "read",
        "TASK_QUEUE_CLIENT_TWO": token_digest(READ_TOKEN),
        "TASK_QUEUE_CLIENT_SCOPES_TWO": "read",
    }
    with pytest.raises(AuthConfigError, match="reuses the token"):
        load_client_tokens(env=env)


@pytest.mark.parametrize("scopes", ["admin", "read,admin", "READ", "write"])
def test_a_scope_outside_the_vocabulary_refuses_to_start(scopes):
    env = {"TASK_QUEUE_CLIENT_X": token_digest(READ_TOKEN), "TASK_QUEUE_CLIENT_SCOPES_X": scopes}
    with pytest.raises(AuthConfigError, match="unknown scope"):
        load_client_tokens(env=env)


@pytest.mark.parametrize("scopes", ["", " ", ",", " , "])
def test_an_empty_scope_list_refuses_to_start(scopes):
    env = {"TASK_QUEUE_CLIENT_X": token_digest(READ_TOKEN), "TASK_QUEUE_CLIENT_SCOPES_X": scopes}
    with pytest.raises(AuthConfigError, match="lists no scopes"):
        load_client_tokens(env=env)


def test_a_digest_with_no_scopes_refuses_to_start():
    with pytest.raises(AuthConfigError, match="no TASK_QUEUE_CLIENT_SCOPES_"):
        load_client_tokens(env={"TASK_QUEUE_CLIENT_X": token_digest(READ_TOKEN)})


def test_scopes_with_no_digest_refuse_to_start():
    with pytest.raises(AuthConfigError, match="no TASK_QUEUE_CLIENT_ token digest"):
        load_client_tokens(env={"TASK_QUEUE_CLIENT_SCOPES_X": "read"})


@pytest.mark.parametrize("key", ["TASK_QUEUE_CLIENT_", "TASK_QUEUE_CLIENT_SCOPES_"])
def test_a_bare_prefix_refuses_to_start(key):
    with pytest.raises(AuthConfigError, match="names no client"):
        load_client_tokens(env={key: "read"})


@pytest.mark.parametrize("suffix", ["OPERATOR", "LEGACY_SHARED"])
def test_a_reserved_channel_refuses_to_start(suffix):
    env = {
        f"TASK_QUEUE_CLIENT_{suffix}": token_digest(READ_TOKEN),
        f"TASK_QUEUE_CLIENT_SCOPES_{suffix}": "read",
    }
    with pytest.raises(AuthConfigError, match="reserved"):
        load_client_tokens(env=env)


def test_a_channel_named_after_an_agent_refuses_to_start():
    env = {
        "TASK_QUEUE_CLIENT_DEVELOPER": token_digest(READ_TOKEN),
        "TASK_QUEUE_CLIENT_SCOPES_DEVELOPER": "read",
    }
    with pytest.raises(AuthConfigError, match="also an agent identity"):
        load_client_tokens(env=env, agent_tokens={AGENT_TOKEN: "developer"})


def test_a_client_digest_equal_to_an_agent_token_refuses_to_start():
    env = {
        "TASK_QUEUE_CLIENT_CLOUDCLI": token_digest(AGENT_TOKEN),
        "TASK_QUEUE_CLIENT_SCOPES_CLOUDCLI": "read",
    }
    with pytest.raises(AuthConfigError, match="same token as an agent"):
        load_client_tokens(env=env, agent_tokens={AGENT_TOKEN: "developer"})


def test_a_bad_client_configuration_stops_the_server_at_import(monkeypatch):
    import src.server as srv  # first import under a clean env; the reload below is the test

    monkeypatch.setenv("TASK_QUEUE_CLIENT_X", "not-a-digest")
    monkeypatch.setenv("TASK_QUEUE_CLIENT_SCOPES_X", "read")
    try:
        with pytest.raises(SystemExit) as exc:
            importlib.reload(srv)
        assert exc.value.code == 1
    finally:
        # Restore a clean module even if the reload did not exit, so later tests do not
        # inherit this configuration.
        monkeypatch.delenv("TASK_QUEUE_CLIENT_X", raising=False)
        monkeypatch.delenv("TASK_QUEUE_CLIENT_SCOPES_X", raising=False)
        importlib.reload(srv)


# --------------------------------------------------------------------------- #
# authorize_client
# --------------------------------------------------------------------------- #


def test_authorize_client_resolves_a_known_token():
    clients = load_client_tokens(env=CLIENT_ENV)
    client = authorize_client({CLIENT_TOKEN_HEADER: READ_TOKEN}, clients)
    assert client is not None
    assert client.channel == "reader"


@pytest.mark.parametrize("headers", [{}, {CLIENT_TOKEN_HEADER: ""}, {CLIENT_TOKEN_HEADER: "nope"}])
def test_authorize_client_refuses_missing_empty_and_unknown(headers):
    clients = load_client_tokens(env=CLIENT_ENV)
    assert authorize_client(headers, clients) is None


def test_the_agent_bearer_header_is_never_read():
    """A client token offered as a bearer is not a client token."""
    clients = load_client_tokens(env=CLIENT_ENV)
    headers = {"Authorization": f"Bearer {BOTH_TOKEN}"}
    assert authorize_client(headers, clients) is None


def test_the_retired_header_is_never_a_credential():
    """v0.12.0: the shared-secret header authenticates nothing, whatever it carries."""
    clients = load_client_tokens(env=CLIENT_ENV)
    assert authorize_client({RETIRED_SECRET_HEADER: RETIRED_SECRET}, clients) is None
    # Not even alongside a client token's worth of nothing.
    headers = {CLIENT_TOKEN_HEADER: "wrong", RETIRED_SECRET_HEADER: RETIRED_SECRET}
    assert authorize_client(headers, clients) is None


def test_the_retired_header_is_logged_when_it_arrives_alone(caplog):
    with caplog.at_level(logging.WARNING, logger="src.auth"):
        assert authorize_client({RETIRED_SECRET_HEADER: RETIRED_SECRET}, {}) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("retired" in m for m in messages)
    assert not any(RETIRED_SECRET in m for m in messages)


def test_retired_secret_set_only_reports():
    assert retired_secret_set({"TASK_QUEUE_API_SECRET": RETIRED_SECRET}) is True
    assert retired_secret_set({}) is False
    assert retired_secret_set({"TASK_QUEUE_API_SECRET": ""}) is False


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """src.server with one agent token and the three test clients."""
    monkeypatch.setenv("TASK_QUEUE_TOKEN_DEVELOPER", AGENT_TOKEN)
    for key, value in CLIENT_ENV.items():
        monkeypatch.setenv(key, value)
    import src.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "QUEUE_DIR", str(tmp_path))
    yield server
    monkeypatch.delenv("TASK_QUEUE_TOKEN_DEVELOPER", raising=False)
    for key in CLIENT_ENV:
        monkeypatch.delenv(key, raising=False)
    importlib.reload(server)


@pytest.fixture
def http(srv):
    with TestClient(srv.mcp.http_app()) as client:
        yield client


def _auth(token: str) -> dict:
    return {CLIENT_TOKEN_HEADER: token}


def _custom_routes(server) -> list[tuple[str, str]]:
    """(method, path) for every non-MCP route the app actually serves. HEAD is GET's."""
    found = []
    for route in server.mcp.http_app().routes:
        if not isinstance(route, Route) or route.path == "/mcp":
            continue
        for method in sorted(route.methods or ()):
            if method != "HEAD":
                found.append((method, route.path))
    return found


def _concrete(path: str) -> str:
    return path.replace("{task_id}", str(uuid.uuid4()))


def test_every_custom_route_declares_a_scope(srv):
    routes = _custom_routes(srv)
    assert routes, "enumeration found no custom routes; the test would be vacuous"
    assert set(routes) == set(srv.ROUTE_SCOPES)


def test_the_route_scopes_are_the_ones_planned(srv):
    for (method, _path), scope in srv.ROUTE_SCOPES.items():
        expected = SCOPE_OPERATOR_WRITE if method == "POST" else SCOPE_READ
        assert scope == expected


def _call(http, method, path, headers):
    return http.request(method, _concrete(path), headers=headers, json={})


def test_no_token_is_401_on_every_custom_route(srv, http):
    for method, path in _custom_routes(srv):
        assert _call(http, method, path, {}).status_code == 401, (method, path)


def test_a_read_token_on_every_write_route_is_403(srv, http):
    writes = [r for r in _custom_routes(srv) if srv.ROUTE_SCOPES[r] == SCOPE_OPERATOR_WRITE]
    assert len(writes) == 8
    for method, path in writes:
        resp = _call(http, method, path, _auth(READ_TOKEN))
        assert resp.status_code == 403, (method, path)
        assert resp.json() == {"ok": False, "error": "scope operator-write required"}


def test_a_write_only_token_on_every_read_route_is_403(srv, http):
    reads = [r for r in _custom_routes(srv) if srv.ROUTE_SCOPES[r] == SCOPE_READ]
    assert len(reads) == 3
    for method, path in reads:
        resp = _call(http, method, path, _auth(WRITE_TOKEN))
        assert resp.status_code == 403, (method, path)
        assert resp.json() == {"ok": False, "error": "scope read required"}


def test_an_agent_bearer_is_401_on_every_custom_route(srv, http):
    """
    FastMCP's AuthenticationMiddleware runs app-wide, so this bearer DOES authenticate the
    request as `developer` at the Starlette layer. The routes must ignore that.
    """
    for method, path in _custom_routes(srv):
        resp = _call(http, method, path, {"Authorization": f"Bearer {AGENT_TOKEN}"})
        assert resp.status_code == 401, (method, path)


def test_the_agent_bearer_really_is_valid(http):
    """Control for the test above: the same bearer opens /mcp, so the 401 is the routes."""
    resp = http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {CLIENT_TOKEN_HEADER: BOTH_TOKEN},
        {"Authorization": f"Bearer {BOTH_TOKEN}"},
    ],
)
def test_a_client_token_is_401_on_mcp(http, headers):
    resp = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**headers, "Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401


# ── GET /tasks ──────────────────────────────────────────────────────────


def _seed(queue_dir, n=1, target="developer"):
    ids = []
    for _ in range(n):
        r = submit_task_handler(
            source_agent="research",
            target_agent=target,
            task_type="build",
            summary="s",
            description="d",
            queue_dir=str(queue_dir),
        )
        ids.append(r["task_id"])
    return ids


def test_list_reports_truncation_and_the_full_match_count(tmp_path, http):
    _seed(tmp_path, 5)
    resp = http.get("/tasks?limit=2", headers=_auth(READ_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["tasks"]) == 2
    assert body["count"] == 5
    assert body["truncated"] is True


def test_list_is_not_truncated_when_everything_fits(tmp_path, http):
    _seed(tmp_path, 3)
    body = http.get("/tasks?limit=3", headers=_auth(READ_TOKEN)).json()
    assert (len(body["tasks"]), body["count"], body["truncated"]) == (3, 3, False)


def test_list_count_is_the_filtered_count(tmp_path, http):
    _seed(tmp_path, 3, target="developer")
    _seed(tmp_path, 2, target="writer")
    body = http.get("/tasks?target_agent=writer&limit=1", headers=_auth(READ_TOKEN)).json()
    assert (len(body["tasks"]), body["count"], body["truncated"]) == (1, 2, True)
    assert body["tasks"][0]["target_agent"] == "writer"


def test_list_serialises_timestamps_as_iso_strings(tmp_path, http):
    _seed(tmp_path)
    task = http.get("/tasks", headers=_auth(READ_TOKEN)).json()["tasks"][0]
    assert isinstance(task["created"], str)
    assert task["created"].startswith("20")
    assert task["queue_location"] == "queue"


def test_list_accepts_the_page_ceiling(tmp_path, http):
    _seed(tmp_path)
    resp = http.get(f"/tasks?limit={LIST_PAGE_MAX}", headers=_auth(READ_TOKEN))
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "query",
    [
        "status=pending",  # not a status; the handler's ValueError must be a 400
        "status=approved,bogus",
        "limit=0",
        f"limit={LIST_PAGE_MAX + 1}",
        "limit=abc",
        "include_archived=maybe",
        "include_dead_letters=2",
        "stauts=approved",  # a misspelt filter must not return the unfiltered queue
    ],
)
def test_list_refuses_bad_queries_with_400(http, query):
    resp = http.get(f"/tasks?{query}", headers=_auth(READ_TOKEN))
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_list_includes_dead_letters_only_when_asked(tmp_path, http):
    tid = _seed(tmp_path)[0]
    src = next(tmp_path.glob("*.yml"))
    dead = tmp_path / "dead-letters"
    dead.mkdir()
    data = yaml.safe_load(src.read_text())
    data["status"] = "failed"
    (dead / src.name).write_text(yaml.dump(data))
    src.unlink()

    plain = http.get("/tasks", headers=_auth(READ_TOKEN)).json()
    assert plain["count"] == 0
    with_dead = http.get("/tasks?include_dead_letters=true", headers=_auth(READ_TOKEN)).json()
    assert [t["id"] for t in with_dead["tasks"]] == [tid]
    assert with_dead["tasks"][0]["queue_location"] == "dead-letters"


def test_the_mcp_tool_output_is_unchanged(tmp_path):
    """The refactor split the handler; the MCP tool still returns a bare, 200-capped list."""
    _seed(tmp_path, 3)
    out = list_tasks_handler(limit=500, queue_dir=str(tmp_path))
    assert isinstance(out, list)
    assert len(out) == 3
    assert LIST_TASKS_MAX == 200


# ── GET /tasks/{id} ─────────────────────────────────────────────────────


def test_get_task_returns_the_record(tmp_path, http):
    tid = _seed(tmp_path)[0]
    resp = http.get(f"/tasks/{tid}", headers=_auth(READ_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["task"]["id"] == tid


def test_get_task_malformed_id_is_400(http):
    assert http.get("/tasks/not-a-uuid", headers=_auth(READ_TOKEN)).status_code == 400


def test_get_task_unknown_id_is_404(http):
    assert http.get(f"/tasks/{uuid.uuid4()}", headers=_auth(READ_TOKEN)).status_code == 404


# ── GET /queue/summary ──────────────────────────────────────────────────


def test_summary_with_a_read_token(tmp_path, http):
    _seed(tmp_path, 2)
    resp = http.get("/queue/summary", headers=_auth(READ_TOKEN))
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "counts": {"submitted": 2},
        "active": 2,
        "total": 2,
        "dead_letters": 0,
    }


# ── channel attribution ─────────────────────────────────────────────────


def _set_status(queue_dir, tid, status, **extra):
    path = next(p for p in queue_dir.glob("*.yml") if tid[:8] in p.name)
    data = yaml.safe_load(path.read_text())
    data["status"] = status
    data.update(extra)
    path.write_text(yaml.dump(data, sort_keys=False))


def _last_history(queue_dir, tid):
    return get_task_handler(task_id=tid, queue_dir=str(queue_dir))["history"][-1]


def _write(http, tid, action, body):
    return http.post(f"/tasks/{tid}/{action}", headers=_auth(BOTH_TOKEN), json=body)


def test_approve_records_the_channel(tmp_path, http):
    tid = _seed(tmp_path)[0]
    assert _write(http, tid, "approve", {}).status_code == 200
    entry = _last_history(tmp_path, tid)
    assert (entry["actor"], entry["channel"]) == ("operator", "matrix-bot")


def test_cancel_records_the_channel(tmp_path, http):
    tid = _seed(tmp_path)[0]
    assert _write(http, tid, "cancel", {}).status_code == 200
    assert _last_history(tmp_path, tid)["channel"] == "matrix-bot"


def test_status_records_the_channel(tmp_path, http):
    tid = _seed(tmp_path)[0]
    assert _write(http, tid, "status", {"status": "approved"}).status_code == 200
    assert _last_history(tmp_path, tid)["channel"] == "matrix-bot"


def test_park_and_unpark_record_the_channel(tmp_path, http):
    tid = _seed(tmp_path)[0]
    assert _write(http, tid, "park", {}).status_code == 200
    assert _last_history(tmp_path, tid)["channel"] == "matrix-bot"
    assert _write(http, tid, "unpark", {}).status_code == 200
    assert _last_history(tmp_path, tid)["channel"] == "matrix-bot"


def test_amend_records_the_channel_on_history_and_amendment(tmp_path, http):
    tid = _seed(tmp_path)[0]
    assert _write(http, tid, "amend", {"amendment": "scope narrowed"}).status_code == 200
    task = get_task_handler(task_id=tid, queue_dir=str(tmp_path))
    assert task["history"][-1]["channel"] == "matrix-bot"
    assert task["payload"]["amendments"][-1]["channel"] == "matrix-bot"


def test_update_records_the_channel_alongside_on_behalf_of(tmp_path, http):
    tid = _seed(tmp_path)[0]
    _set_status(tmp_path, tid, "in-progress")
    body = {"status": "completed", "on_behalf_of": "developer", "note": "sweep"}
    assert _write(http, tid, "update", body).status_code == 200
    entry = _last_history(tmp_path, tid)
    assert (entry["actor"], entry["on_behalf_of"], entry["channel"]) == (
        "operator",
        "developer",
        "matrix-bot",
    )


def test_requeue_records_the_channel(tmp_path, http):
    tid = _seed(tmp_path)[0]
    src = next(tmp_path.glob("*.yml"))
    dead = tmp_path / "dead-letters"
    dead.mkdir()
    data = yaml.safe_load(src.read_text())
    data["status"] = "failed"
    (dead / src.name).write_text(yaml.dump(data))
    src.unlink()
    assert _write(http, tid, "requeue", {}).status_code == 200
    assert _last_history(tmp_path, tid)["channel"] == "matrix-bot"


def test_every_write_route_has_a_channel_test(srv):
    """If a write route is added, this fails until it gets a channel test above."""
    covered = {"approve", "cancel", "status", "park", "unpark", "amend", "update", "requeue"}
    writes = {
        path.rsplit("/", 1)[-1]
        for (method, path), scope in srv.ROUTE_SCOPES.items()
        if scope == SCOPE_OPERATOR_WRITE
    }
    assert writes == covered


def test_an_mcp_tool_write_records_no_channel(tmp_path, srv):
    tid = _seed(tmp_path)[0]
    srv.cancel_task(task_id=tid, actor="operator")
    assert "channel" not in _last_history(tmp_path, tid)


# ── the retired shared secret (removed in v0.12.0) ──────────────────────


def test_setting_the_retired_secret_again_re_enables_nothing(tmp_path, monkeypatch):
    """
    The plan's requirement for v0.12.0: TASK_QUEUE_API_SECRET, set again at startup, must
    not open any route. Set before the reload, so it is in the environment the server
    starts with, exactly as a restored env file would put it there.
    """
    monkeypatch.setenv("TASK_QUEUE_API_SECRET", RETIRED_SECRET)
    for key, value in CLIENT_ENV.items():
        monkeypatch.setenv(key, value)
    import src.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "QUEUE_DIR", str(tmp_path))
    try:
        tid = _seed(tmp_path)[0]
        with TestClient(server.mcp.http_app()) as c:
            for method, path in _custom_routes(server):
                resp = c.request(
                    method,
                    path.replace("{task_id}", tid),
                    headers={RETIRED_SECRET_HEADER: RETIRED_SECRET},
                    json={},
                )
                assert resp.status_code == 401, (method, path)
        assert get_task_handler(task_id=tid, queue_dir=str(tmp_path))["status"] == "submitted"
    finally:
        monkeypatch.delenv("TASK_QUEUE_API_SECRET", raising=False)
        for key in CLIENT_ENV:
            monkeypatch.delenv(key, raising=False)
        importlib.reload(server)


def test_the_retired_channel_name_stays_reserved():
    """v0.11.0 history carries `channel: legacy-shared`; no client may take the name."""
    assert RETIRED_CHANNEL == "legacy-shared"
    env = {
        "TASK_QUEUE_CLIENT_LEGACY_SHARED": token_digest(READ_TOKEN),
        "TASK_QUEUE_CLIENT_SCOPES_LEGACY_SHARED": "read",
    }
    with pytest.raises(AuthConfigError, match="reserved"):
        load_client_tokens(env=env)
