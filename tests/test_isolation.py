"""
Guards for tests/conftest.py (vikunja#568).

If the strip in conftest stops working, the other suites keep passing: they set what they
need with monkeypatch and ignore the rest. Only these tests notice. They name variables,
never values, so a failure here cannot print the credential it is reporting.
"""

import importlib
import os

from tests.conftest import _is_credential


def test_no_credential_variable_survives_into_the_suite():
    leaked = sorted(k for k in os.environ if _is_credential(k))
    assert leaked == [], f"credential variables reached the test process: {leaked}"


def test_a_reloaded_server_sees_no_ambient_agent_token():
    import src.server as srv

    importlib.reload(srv)
    assert len(srv._agent_tokens) == 0


def test_the_strip_covers_every_credential_family():
    for name in (
        "TASK_QUEUE_TOKEN_DEVELOPER",
        "TASK_QUEUE_CLIENT_CLOUDCLI",
        "TASK_QUEUE_CLIENT_SCOPES_CLOUDCLI",
        "TASK_QUEUE_API_SECRET",
    ):
        assert _is_credential(name), name
    # The endpoint URL and the queue dir are configuration, not credentials.
    assert not _is_credential("TASK_QUEUE_API")
    assert not _is_credential("TASK_QUEUE_DIR")
