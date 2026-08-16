"""
Per-agent bearer token authentication for the MCP tool path.

Until v0.7.0 the MCP transport had no auth at all. Only the seven HTTP *control*
routes were gated (TASK_QUEUE_API_SECRET); a comment in server.py referred to "the MCP
auth middleware", but none was ever configured. The port is published on loopback *and*
the container joins a shared Docker network, so any local process and any container on
that network could call any tool and assert any `actor` — including "operator", which the
update_task ownership check explicitly exempts. That made `completed_by` and
`history[].actor` claims rather than evidence. (vikunja#387)

Each agent gets a distinct token, so the token both authenticates the caller and
identifies it. That is why there is no separate identity header: once an agent holds a
token it can also set any header it likes on a direct request, so a header-derived
identity would be a strictly weaker second channel competing with the token-derived one.
One source of identity, not two.

Configuration — one env var per agent:

    TASK_QUEUE_TOKEN_DEVELOPER=<token>
    TASK_QUEUE_TOKEN_DOC_HEALTH=<token>

The suffix maps to the agent name lowercased with underscores turned back into hyphens
(DOC_HEALTH -> doc-health). Agent names are hyphenated by convention, never underscored,
so that round-trip is unambiguous. An agent name containing a literal underscore cannot be
expressed and would silently arrive hyphenated.

Threat model. The agents this serves are not assumed adversarial. This contains a
*mistaken or prompt-injected* agent acting through its own tool surface, and it makes the
audit trail mean what it says. It is deliberately not a boundary against an agent that
goes looking for credentials: where agents hold a shell tool and run as the same OS user
that owns the secret files, any token on the host is readable by any of them. Raising that
floor needs per-agent OS users or a credential broker, and is out of scope here.
"""

import logging
import os

from fastmcp.server.auth import StaticTokenVerifier

logger = logging.getLogger(__name__)

TOKEN_ENV_PREFIX = "TASK_QUEUE_TOKEN_"

# Short tokens are brute-forceable from anywhere the port is reachable, and are the kind of
# thing a placeholder like "changeme" would sail past. Tokens are generated with
# `secrets.token_urlsafe(32)` (43 chars), so this only ever catches a misconfiguration.
MIN_TOKEN_LENGTH = 16

# "operator" is the identity the HTTP control routes assert, gated by TASK_QUEUE_API_SECRET.
# It must never be reachable from the agent-facing MCP transport: the update_task ownership
# check exempts it from every ownership rule, so a token minted for it would hand its holder
# the whole queue. Refused at load time, not at call time — a misconfiguration should fail
# the deploy, not wait for someone to exercise it.
RESERVED_IDENTITIES = frozenset({"operator"})


class AuthConfigError(RuntimeError):
    """Raised for a token configuration that would fail open or silently mis-attribute."""


def _identity_from_env_key(key: str) -> str:
    """TASK_QUEUE_TOKEN_DOC_HEALTH -> doc-health"""
    return key[len(TOKEN_ENV_PREFIX) :].lower().replace("_", "-")


def load_agent_tokens(env: dict[str, str] | None = None) -> dict[str, str]:
    """
    Build the token -> agent-identity map from the environment.

    Returns {} when no tokens are configured. Callers decide whether that is fatal:
    it is on the HTTP transport, and is not on stdio, which has no network surface.

    Raises AuthConfigError on a configuration that would fail open or mis-attribute:
    a token shorter than MIN_TOKEN_LENGTH, a token shared by two agents, or a token
    minted for a reserved identity.
    """
    env = os.environ if env is None else env

    tokens: dict[str, str] = {}
    for key, value in sorted(env.items()):
        if not key.startswith(TOKEN_ENV_PREFIX) or key == TOKEN_ENV_PREFIX:
            continue

        identity = _identity_from_env_key(key)
        token = value.strip()

        if not token:
            # An env var present but empty is almost always a provisioning miss (a
            # secrets file that did not interpolate). Treat it as fatal rather than
            # quietly leaving that agent unable to authenticate.
            raise AuthConfigError(f"{key} is set but empty — refusing to start.")

        if len(token) < MIN_TOKEN_LENGTH:
            raise AuthConfigError(
                f"{key} is too short ({len(token)} chars, need >= {MIN_TOKEN_LENGTH})."
            )

        if identity in RESERVED_IDENTITIES:
            raise AuthConfigError(
                f"{key} would mint a token for reserved identity {identity!r}. "
                "The operator identity is reachable only from the HTTP control routes."
            )

        if token in tokens:
            # Two agents sharing a token collapses in the dict and silently attributes
            # both agents' actions to whichever one loaded last — the exact failure the
            # per-agent token exists to prevent.
            raise AuthConfigError(
                f"{key} reuses the token already assigned to {tokens[token]!r}. "
                "Every agent needs a distinct token or attribution is meaningless."
            )

        tokens[token] = identity

    return tokens


def build_verifier(tokens: dict[str, str]) -> StaticTokenVerifier | None:
    """
    Wrap the token map in FastMCP's StaticTokenVerifier, or None when unconfigured.

    StaticTokenVerifier's docstring warns against production use because it holds tokens
    in plaintext. That is the accepted trade here, as it is for githost-mcp: forge has no
    authorization server, the tokens are static shared secrets sourced from a 0600 env
    file, and the alternative is the status quo of no authentication whatsoever.

    The `sub` claim carries the agent identity. Nothing reads it yet — binding `actor` to
    it is the next phase — but it is set here so the token is self-describing from the
    moment it is minted. Note StaticTokenVerifier does NOT populate AccessToken.subject;
    it only echoes this claims dict back, so `sub` must be read from claims, not subject.
    """
    if not tokens:
        return None
    return StaticTokenVerifier(
        tokens={
            token: {"sub": identity, "client_id": identity, "scopes": []}
            for token, identity in tokens.items()
        }
    )
