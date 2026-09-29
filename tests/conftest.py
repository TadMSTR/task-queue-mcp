"""
Suite-wide isolation from the credentials in the ambient environment (vikunja#568).

src.server builds its token maps from os.environ at import time, and most suites reload it.
On a host that runs the real service, the shell a developer runs pytest from carries the
real TASK_QUEUE_TOKEN_* values, and until this file existed a failing assertion on a token
map printed them verbatim. Ambient values also changed what was under test: a reload picked
up every real agent token, so a test meaning "one agent configured" was really testing ten.

The strip runs in pytest_configure, not in a fixture. Fixtures run after collection, and
collection imports the test modules. Nothing today reads the token variables at module
import, but a test module that did would read them before any session fixture could remove
them. pytest_configure runs before collection starts, so no import can come first.

Tests that need a variable set it with monkeypatch.setenv, which restores the stripped state
afterwards.
"""

import os

# Every prefix that names a credential this server reads, or has read. Exact names are
# listed as well as prefixes: TASK_QUEUE_API_SECRET is the v0.10.0 shared secret.
_CREDENTIAL_PREFIXES = ("TASK_QUEUE_TOKEN_", "TASK_QUEUE_CLIENT_")
_CREDENTIAL_NAMES = frozenset({"TASK_QUEUE_API_SECRET"})


def _is_credential(name: str) -> bool:
    return name in _CREDENTIAL_NAMES or name.startswith(_CREDENTIAL_PREFIXES)


def pytest_configure(config):
    for name in [k for k in os.environ if _is_credential(k)]:
        del os.environ[name]
