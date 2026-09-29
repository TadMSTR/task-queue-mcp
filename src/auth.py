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

import hashlib
import hmac
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

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


# --------------------------------------------------------------------------- #
# Client tokens for the HTTP control and read routes
# --------------------------------------------------------------------------- #
#
# The custom routes are the operator surface: the CloudCLI plugin, the Matrix bot, and
# (from operator-panel part 4) the panel. Until v0.11.0 one shared secret,
# TASK_QUEUE_API_SECRET, gated all of them, and the same value sat in 23 processes'
# environments, including every Claude session CloudCLI launched (vikunja#396). Nothing
# could read the queue over HTTP without also being able to write it.
#
# Each client now has its own token and its own scopes:
#
#     TASK_QUEUE_CLIENT_CLOUDCLI=sha256:<64 lowercase hex>
#     TASK_QUEUE_CLIENT_SCOPES_CLOUDCLI=read,operator-write
#
# THE SERVER HOLDS DIGESTS, NEVER TOKENS. This container runs as 1000:1000 and its env file
# is readable by `ted`, which every agent runs as. Plaintext here would hand every agent
# the panel's credential and make it a label rather than a boundary. A sha256 of a 32-byte
# random token is not reversible, so reading this configuration yields nothing a request
# can use. The plaintext lives with the client, and for the panel under its own UID.
#
# That is also the limit of what the cloudcli and matrix-bot tokens buy. Those clients run
# as `ted`, so their token files are readable by any agent. They gain revocability and
# attribution, and they leave the environment of every process on the host. They are not
# a boundary, and nothing here claims they are.
#
# Tokens arrive in X-Task-Queue-Token, never in Authorization. FastMCP installs its
# AuthenticationMiddleware for the whole app, not only /mcp. A bearer on a custom route is
# offered to the agent-token verifier, and a valid agent bearer authenticates the request.
# These routes ignore that result entirely (see authorize_client), and keeping client tokens
# out of the Authorization header means no FastMCP change can alter how they are parsed.

CLIENT_TOKEN_HEADER = "X-Task-Queue-Token"
CLIENT_ENV_PREFIX = "TASK_QUEUE_CLIENT_"
CLIENT_SCOPES_ENV_PREFIX = "TASK_QUEUE_CLIENT_SCOPES_"

SCOPE_READ = "read"
SCOPE_OPERATOR_WRITE = "operator-write"
# Closed vocabulary. Neither scope implies the other: a client that only shows counts
# gets `read` alone, and a write-only client cannot read the queue it writes to.
VALID_SCOPES = frozenset({SCOPE_READ, SCOPE_OPERATOR_WRITE})

DIGEST_PREFIX = "sha256:"
_HEX = frozenset("0123456789abcdef")

# v0.11.0 only. The shared secret still works under this channel so the server can deploy
# before its clients move. v0.12.0 deletes it.
LEGACY_SECRET_ENV = "TASK_QUEUE_API_SECRET"
LEGACY_SECRET_HEADER = "X-Task-Queue-Secret"
LEGACY_CHANNEL = "legacy-shared"

# Channel names no client may take. `operator` is the actor every client writes as, and a
# channel of that name would make the history entry say nothing. `legacy-shared` belongs to
# the transitional shared-secret path.
RESERVED_CHANNELS = frozenset({OPERATOR_ACTOR, LEGACY_CHANNEL})


@dataclass(frozen=True)
class Client:
    """An authenticated HTTP client: the channel it writes as, and what it may do."""

    channel: str
    scopes: frozenset[str]


def token_digest(token: str) -> str:
    """The form a client token is stored in: `sha256:<hex>` of its UTF-8 bytes."""
    return DIGEST_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _channel_from_env_key(key: str, prefix: str) -> str:
    """TASK_QUEUE_CLIENT_MATRIX_BOT -> matrix-bot, the same rule agent names use."""
    return key[len(prefix) :].lower().replace("_", "-")


def _valid_digest(value: str) -> bool:
    if not value.startswith(DIGEST_PREFIX):
        return False
    hexpart = value[len(DIGEST_PREFIX) :]
    return len(hexpart) == 64 and set(hexpart) <= _HEX


def load_client_tokens(
    env: dict[str, str] | None = None,
    agent_tokens: dict[str, str] | None = None,
) -> dict[str, Client]:
    """
    Build the digest -> Client map from the environment.

    Zero clients is a valid configuration: every custom route then refuses every request,
    and the MCP path is unaffected.

    Raises AuthConfigError, and the server refuses to start, on anything that would fail
    open or mis-attribute a write:
      - a digest that is not `sha256:` plus 64 lowercase hex characters
      - one digest configured for two clients
      - a digest with no scopes line, or a scopes line with no digest
      - an empty scope list, or a scope outside VALID_SCOPES
      - a channel name that is reserved or is an agent identity
      - a digest equal to the digest of an agent token, so one secret would be both
    None of these messages contain a digest or a token.
    """
    env = os.environ if env is None else env
    agent_tokens = agent_tokens or {}
    agent_identities = set(agent_tokens.values())
    agent_digests = {token_digest(t) for t in agent_tokens}

    digests: dict[str, str] = {}  # channel -> digest
    scopes: dict[str, frozenset[str]] = {}  # channel -> scopes

    for key, raw in sorted(env.items()):
        # The scopes prefix is the longer of the two and shares the digest prefix, so it
        # has to be tested first. A client named SCOPES_<X> therefore cannot be expressed.
        if key.startswith(CLIENT_SCOPES_ENV_PREFIX):
            if key == CLIENT_SCOPES_ENV_PREFIX:
                raise AuthConfigError(f"{key} names no client.")
            channel = _channel_from_env_key(key, CLIENT_SCOPES_ENV_PREFIX)
            requested = [s.strip() for s in raw.split(",") if s.strip()]
            if not requested:
                raise AuthConfigError(f"{key} is set but lists no scopes.")
            unknown = sorted(set(requested) - VALID_SCOPES)
            if unknown:
                raise AuthConfigError(
                    f"{key} names unknown scope(s) {unknown}. Valid scopes: {sorted(VALID_SCOPES)}."
                )
            scopes[channel] = frozenset(requested)
            continue

        if not key.startswith(CLIENT_ENV_PREFIX):
            continue
        if key == CLIENT_ENV_PREFIX:
            raise AuthConfigError(f"{key} names no client.")

        channel = _channel_from_env_key(key, CLIENT_ENV_PREFIX)
        value = raw.strip()
        if not _valid_digest(value):
            # Deliberately says nothing about the value. The likeliest mistake is pasting
            # the plaintext token here, and echoing it would put it in the container log.
            raise AuthConfigError(
                f"{key} is not a {DIGEST_PREFIX}<64 lowercase hex> digest. "
                "The server stores token digests, never tokens."
            )
        digests[channel] = value

    unscoped = sorted(set(digests) - set(scopes))
    if unscoped:
        raise AuthConfigError(
            f"client(s) {unscoped} have a token digest but no {CLIENT_SCOPES_ENV_PREFIX} line."
        )
    orphaned = sorted(set(scopes) - set(digests))
    if orphaned:
        raise AuthConfigError(
            f"client(s) {orphaned} have scopes but no {CLIENT_ENV_PREFIX} token digest."
        )

    clients: dict[str, Client] = {}
    for channel in sorted(digests):
        if channel in RESERVED_CHANNELS:
            raise AuthConfigError(f"client channel {channel!r} is reserved.")
        if channel in agent_identities:
            raise AuthConfigError(
                f"client channel {channel!r} is also an agent identity. A history entry "
                "must not be able to read as either."
            )
        digest = digests[channel]
        if digest in agent_digests:
            raise AuthConfigError(
                f"client {channel!r} uses the same token as an agent. Client and agent "
                "tokens must be distinct."
            )
        if digest in clients:
            raise AuthConfigError(
                f"client {channel!r} reuses the token of {clients[digest].channel!r}. "
                "Every client needs a distinct token or attribution is meaningless."
            )
        clients[digest] = Client(channel=channel, scopes=scopes[channel])

    return clients


def legacy_secret_configured(env: dict[str, str] | None = None) -> bool:
    """
    Whether the legacy shared secret is set AND long enough to accept.

    A secret shorter than MIN_TOKEN_LENGTH is treated as unset: it would otherwise grant
    both scopes to anything that can guess it. The same floor applies to agent tokens.
    """
    env = os.environ if env is None else env
    return len(env.get(LEGACY_SECRET_ENV, "")) >= MIN_TOKEN_LENGTH


def legacy_secret_too_short(env: dict[str, str] | None = None) -> bool:
    """Set, but refused for being under MIN_TOKEN_LENGTH. For the startup warning."""
    env = os.environ if env is None else env
    value = env.get(LEGACY_SECRET_ENV, "")
    return bool(value) and len(value) < MIN_TOKEN_LENGTH


def authorize_client(
    headers: Mapping[str, str],
    clients: dict[str, Client],
    env: dict[str, str] | None = None,
) -> Client | None:
    """
    The client a custom-route request authenticates as, or None.

    Reads X-Task-Queue-Token and nothing else that FastMCP knows about. request.user and
    get_access_token() are deliberately never consulted: FastMCP's middleware may have
    authenticated an agent bearer on this request, and an agent identity grants nothing on
    the operator surface.

    The presented token is hashed and compared against EVERY configured digest with
    hmac.compare_digest, without stopping at a match, so the time taken does not depend on
    which client matched or how far down the list it was.

    If X-Task-Queue-Token is present it decides the outcome alone; the legacy header is only
    consulted when it is absent, so a bad new token cannot fall back to the old secret.
    """
    presented = headers.get(CLIENT_TOKEN_HEADER)
    if presented is not None:
        if not presented:
            return None
        digest = token_digest(presented).encode("utf-8")
        found: Client | None = None
        for configured, client in clients.items():
            if hmac.compare_digest(digest, configured.encode("utf-8")):
                found = client
        return found

    env = os.environ if env is None else env
    provided = headers.get(LEGACY_SECRET_HEADER)
    if provided is None or not legacy_secret_configured(env):
        return None
    secret = env[LEGACY_SECRET_ENV]
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str operands, and a
    # malformed header must not escape as a 500. (audit L-02)
    if not hmac.compare_digest(provided.encode("utf-8"), secret.encode("utf-8")):
        return None
    logger.warning(
        "control-api: request authenticated with the deprecated shared secret "
        "(%s, channel %s). Move this client to %s; v0.12.0 removes the shared secret.",
        LEGACY_SECRET_HEADER,
        LEGACY_CHANNEL,
        CLIENT_TOKEN_HEADER,
    )
    return Client(channel=LEGACY_CHANNEL, scopes=VALID_SCOPES)
