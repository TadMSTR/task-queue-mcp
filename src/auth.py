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

import hmac
import logging
import os

from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token

from src.tools.queue import OPERATOR_ACTOR

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
#
# Derived from queue.OPERATOR_ACTOR rather than re-spelling the literal. This set and that
# constant have to mean the same thing or the guarantee inverts: require_operator_surface
# refuses every resolved identity *because* no token can carry the operator name, so if
# these two drifted apart a token could be minted for the exact identity the handlers
# exempt. The audit flagged the server.py/queue.py pair; this was the third copy.
RESERVED_IDENTITIES = frozenset({OPERATOR_ACTOR})


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

    The `sub` claim carries the agent identity — resolve_identity() reads it back to
    derive `actor`. Note StaticTokenVerifier does NOT populate AccessToken.subject; it
    only echoes this claims dict back, so `sub` must be read from claims, not subject.
    """
    if not tokens:
        return None
    return StaticTokenVerifier(
        tokens={
            token: {"sub": identity, "client_id": identity, "scopes": []}
            for token, identity in tokens.items()
        }
    )


def resolve_identity() -> str | None:
    """
    The authenticated agent for the request in flight, or None when auth is not active.

    None means "no authenticated identity available" — on stdio, in unit tests, or on the
    HTTP control routes, whose custom_route handlers sit outside the transport's auth
    provider. It never means "operator": a None must not be read as permission to skip an
    ownership check.
    """
    token = get_access_token()
    if token is None:
        return None
    identity = (token.claims or {}).get("sub") or token.client_id
    return identity or None


def bind_actor(claimed: str | None) -> tuple[bool, str]:
    """
    Derive the acting identity for an MCP tool call. Returns (ok, actor_or_error).

    The authenticated identity always wins. `claimed` survives as a tool argument only so
    existing callers keep working and so a mismatch is an explicit refusal rather than a
    silent rewrite — an agent passing someone else's name has a bug worth surfacing, and
    quietly correcting it would hide that.

    When no identity is resolved (stdio, tests, unauthenticated server) the claimed value
    is used as-is. That is not a hole being left open: the network surface is closed by
    requiring auth on the HTTP transport, which refuses to start without tokens. It keeps
    this function honest about the one thing it can actually know.
    """
    resolved = resolve_identity()

    if resolved is None:
        if not claimed or not claimed.strip():
            return False, "actor is required when the server is running without auth"
        return True, claimed

    # compare_digest over two short agent names is not about timing — it is about not
    # growing a second, subtly different string comparison for identity anywhere.
    if claimed and not hmac.compare_digest(claimed, resolved):
        return False, (
            f"actor {claimed!r} does not match the authenticated identity {resolved!r}. "
            "actor is derived from your bearer token and cannot be asserted."
        )

    return True, resolved


def require_operator_surface(tool: str) -> str | None:
    """
    Refuse an operator-only tool when an agent identity is authenticated.

    Returns an error string to return to the caller, or None if the call may proceed.

    `set_task_status` and `cancel_task` are operator-facing by documentation, and were
    reachable by every agent in practice. There is no agent token that resolves to
    `operator` — load_agent_tokens refuses to mint one — so any resolved identity here is
    an agent, and the answer is always no. When nothing is resolved the call is on the
    control routes, stdio, or a test, and proceeds as before.
    """
    resolved = resolve_identity()
    if resolved is None:
        return None
    return (
        f"{tool} is operator-only and is not reachable with an agent identity "
        f"({resolved!r}). Use the HTTP control routes, which are the operator surface."
    )
