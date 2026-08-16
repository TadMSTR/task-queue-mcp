# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.8.2] - 2026-08-16

### Fixed
- **The operator identity was spelled independently in three places** (2026-08-16 audit,
  LOW). `queue.OPERATOR_ACTOR`, `server.OPERATOR_ACTOR`, and `auth.RESERVED_IDENTITIES`
  each held their own `"operator"` literal with nothing tying them together.

  Not exploitable as shipped — every caller pins or enforces correctly — but drift would
  raise neither an import error nor a type error, and fails silently in *both* directions:
  strip the HTTP control routes of the exemption every ownership check grants them, or let
  a token be minted for the very identity those checks exempt.

  `src/tools/queue.py` is now the single source of truth; `server.py` and `auth.py` import
  it. The audit suggested `auth.py` as the home — it lives in `queue.py` instead because
  that module is the domain layer and depends only on the standard library plus yaml, while
  `auth.py` pulls in fastmcp; homing it there would make the queue logic transitively
  depend on a server framework for a string. `queue → auth → server` has no cycle.

  The audit found two of the three sites. `RESERVED_IDENTITIES` was the third, and
  arguably the most coupled: it is what guarantees no agent token can carry the operator
  name, which is the assumption `require_operator_surface` rests on.

  Guarded by a **source-level** regression test. The obvious runtime check
  (`server.OPERATOR_ACTOR is queue.OPERATOR_ACTOR`) is vacuous — CPython interns
  identifier-like literals, so two separately-compiled `= "operator"` assignments are the
  same object and `is` passes. Verified rather than assumed; the test reads the files, and
  was confirmed to fail when a duplicate literal is reintroduced.

## [0.8.1] - 2026-08-16

### Fixed
- **`list_tasks` hid open work once it passed its TTL** (vikunja#395). The TTL filter
  exempted only `parked`, so `submitted`, `approved`, `pending-approval`, `in-progress`
  and `routing-failed` tasks silently disappeared from every listing after `ttl_days`
  while still sitting on disk waiting for someone.

  That is not a stale-item guard, it is a blind spot, and it had already cost something
  measurable: a queue sweep found **17** stranded tasks after this tool reported 13 — the
  four oldest had aged out of the listing used to count them.

  All non-terminal statuses are now exempt. Terminal records still age out of the default
  view, because the original reasoning holds for finished work: nothing that is still
  someone's responsibility should be hidden by a clock, and an agent handed a stale open
  task can judge it, whereas nobody can act on a task they cannot see.

- **The auto-close note now carries the return task's summary.** The submit-time auto-close
  always wins the race against the answering agent's own explicit close — it fires during
  `submit_task` of the return task, which necessarily precedes that call — so its note is
  what the history actually records and the agent's own wording never lands. It read
  `auto-closed: return task <uuid> submitted`, which says a reply happened but not what it
  said. It now appends the return task's summary, so the trail records the outcome.

### Changed
- Two tests retargeted rather than deleted, since TTL filtering still exists for terminal
  tasks. `test_list_excludes_expired_tasks` asserted the exact behaviour vikunja#395 calls
  a bug; it becomes `test_list_excludes_expired_terminal_tasks`. The
  "unparked, it expires out of the listing" assertion in `test_parked_task_survives_its_ttl`
  is dropped — every non-terminal status is exempt now, so the parked exemption is subsumed
  by the general rule. The test remains as a regression guard on parked specifically, which
  predates the general rule and should survive any future narrowing of it.

## [0.8.0] - 2026-08-16

### Added
- **`actor` is now derived from the bearer token instead of taken from the caller.** v0.7.0
  authenticated the caller; this binds identity to that authentication. A mismatched
  `actor` is refused rather than silently corrected — passing another agent's name is a
  bug worth surfacing, and quietly rewriting it would hide it. Omitting `actor` is fine
  and fills it in from the token.

  This covers `source_agent` on `submit_task`, which is an identity claim rather than a
  label: the submit-time auto-close decides whether to fire from `source_agent` /
  `target_agent`, so binding only the `actor` arguments would have left a route to
  terminally closing another agent's task without ever calling `update_task`. Terminal
  statuses are immutable, so that is not a recoverable mistake.

- **An ownership rule for `park_task` / `unpark_task`** — the task's `target_agent` or the
  operator. Park is "not now, but don't lose this" about your own queue, not a lever over
  someone else's.

- **`POST /tasks/{id}/update` — the audited operator sweep.** Closing the MCP path closed
  the *dishonest* way to tidy up another agent's stranded task (pass their name as
  `actor`; 17 were swept that way in the release before this one, honestly annotated, and
  only possible because `actor` was a free string). Nothing else reached it —
  `set_task_status` cannot make terminal transitions and the `update_task` tool now
  demands the resolved identity — so without a replacement every future stray would need
  the operator by hand.

  Pass `on_behalf_of` naming the agent whose task it is. It is verified against the task's
  real `target_agent` (a mismatch is a 400) and **both** names are written to history:
  `actor: operator` alongside `on_behalf_of: developer`. A sweep should read as a sweep
  years later, not as the agent having quietly closed its own work.

- **An adversarial test suite** (`tests/test_adversarial.py`) — agent A attempting to act
  on agent B's task by every route available to it: spoofed `actor`, asserted `operator`,
  own identity on someone else's task, spoofed `source_agent`, the auto-close path,
  `set_task_status` override, `cancel_task`, and `park_task`. Each must fail, and the
  honest paths are asserted alongside them so the model cannot be made strict by making it
  wrong.

### Changed
- **`set_task_status` and `cancel_task` are operator-only** and refuse any authenticated
  agent identity, pointing the caller at the control routes. `set_task_status` because its
  `allow_override` path moves a task between any two non-terminal statuses — how a task
  gets walked around a transition rule instead of satisfying it. `cancel_task` because it
  is a terminal, irreversible judgement about someone else's work; an agent abandoning its
  own task marks it `failed` with a reason.
- **`actor` is pinned to `operator` on all six control-route mutations** rather than
  defaulted via `body.get("actor", "operator")`. The default was correct in practice, but
  it made the operator identity something a caller inherited by omission rather than
  something anyone chose.

### Fixed
- Two tests were retargeted rather than deleted, since the behaviour they covered moved
  rather than disappeared. `test_amend_target_agent_rejected_400` asserted a 400 from the
  handler's authorization rule via HTTP; with the actor pinned, the body's actor never
  reaches the handler and that scenario is no longer expressible over HTTP. It now asserts
  the stronger property at that layer — a caller cannot choose an identity there at all.
  The original rule remains covered at `test_queue.py::test_amend_target_agent_rejected`.

### Notes
- The containment is bounded and worth stating plainly: this contains a *mistaken or
  prompt-injected* agent acting through its own tool surface. It is not a boundary against
  an agent that goes looking for credentials, since the control-route shared secret is
  readable wherever agents run as the user that owns it. That is tracked separately and
  needs per-agent OS users or a credential broker, not a change here.

## [0.7.0] - 2026-08-16

### Added
- **Bearer token authentication on the MCP tool path.** The transport had none. Only the
  seven HTTP *control* routes were gated; a comment in `server.py` referred to "the MCP
  auth middleware", but no auth provider was ever configured on
  `mcp.run(transport="streamable-http", ...)`.

  An unauthenticated `initialize` to `/mcp` returned HTTP 200 with an `mcp-session-id`
  issued — no bearer token, no shared secret. The port is published *and* the container
  joins a shared Docker network, so this was reachable from well beyond the local host.
  Any caller could invoke any tool while asserting any `actor`, including `operator`,
  which every ownership check explicitly exempts. `completed_by` and `history[].actor`
  were therefore claims rather than evidence, and the v0.5.0 `update_task` ownership
  check was advisory. (vikunja#387)

  Each agent now gets a distinct token via `TASK_QUEUE_TOKEN_<AGENT>`, so the token both
  authenticates the caller and identifies it. There is no separate identity header on
  purpose: an agent holding a token can set any header it likes on a direct request, so a
  header-derived identity would be a weaker second channel competing with the
  token-derived one.

  The configuration fails closed in every direction it can be got wrong. The HTTP
  transport refuses to start with no tokens at all; `load_agent_tokens` refuses an empty
  value, a token under 16 characters, a token shared between two agents (which would
  collapse in the identity map and mis-attribute both), and a token minted for the
  reserved `operator` identity.

### Changed
- The control routes keep `TASK_QUEUE_API_SECRET` as their only gate and are deliberately
  outside the new bearer auth — `custom_route` handlers bypass the transport's auth
  provider, and these routes are the operator surface. The CloudCLI plugin and Matrix bot
  need no changes. The stale comment claiming these routes sat outside an MCP auth
  middleware that did not exist has been corrected.
- README trust model rewritten. It previously argued loopback was a sufficient boundary
  and described the MCP endpoint as "limited to LAN/loopback"; both were wrong.

### Notes
- `actor` is still a caller-supplied parameter. This release authenticates the caller but
  does not yet derive `actor` from the authenticated identity; until it does, the
  ownership checks remain integrity controls over a self-asserted string.
- **Cutover is ordered.** Callers must hold their token *before* the server starts
  demanding one: provision the per-agent tokens and roll out the client config first,
  then restart this server. The reverse order locks every caller out at once.

## [0.6.1] - 2026-08-16

### Fixed
- **The auto-close fired on forward requests, not just returns — and closed a live
  in-flight build task within an hour of v0.6.0 shipping.**

  `originating_task_id` is overloaded. On a return task it means "this answers that
  request". On a *forward* request it means "inherit workflow_mode from this parent", and
  `shared-build-pre-audit` Step 4 has always told the build agent to pass its own build task
  when filing an audit request, for exactly that reason.

  v0.6.0 checked only `parent.target_agent == source_agent`, which cannot tell those apart:
  the build task targets `developer` and `developer` is the submitter. So the first audit
  request filed after the release auto-closed the build it belonged to. Terminal tasks are
  immutable, so the task could not be reopened.

  The auto-close now requires the full **return shape** — both halves:

  ```
  parent.target_agent == new.source_agent    # I did the parent's work
  parent.source_agent == new.target_agent    # and I am answering the asker
  ```

  A genuine return is symmetric (audit task `developer→security`, return
  `security→developer`). A forward request is not (build task `research→developer`, request
  `developer→security` — `research != security`). The forward case is now logged at info
  and skipped.

  Strictly narrower than v0.6.0; it cannot newly close anything that was previously safe.
  Two regression tests, both verified red against the v0.6.0 source.

## [0.6.0] - 2026-08-16

The theme: the agent that does the work should be the agent that closes the record of it.
`update_task`'s v0.5.0 ownership check made that the rule; nothing made it reachable.
14 audit-request tasks had accumulated at `approved` between 2026-07-19 and 2026-08-15,
each one a finished audit nobody could close. vikunja#382.

### Added
- **Auto-close on return-task submission.** `submit_task` with an `originating_task_id`
  now closes that parent, when — and only when — the parent exists, is unarchived, is at
  `approved` or `in-progress`, and **targets the submitting agent**. Submitting the return
  task *is* closing the request. The response carries `auto_closed_task_id` when it fires.

  `parent.target_agent == source_agent` is the whole bound on this feature, and it is
  checked explicitly rather than deferring to `update_task_handler`'s ownership check —
  that one also admits `operator`, so a caller submitting as `source_agent="operator"`
  would otherwise be able to close anybody's task.

  The eligible-source set is a literal `{"approved", "in-progress"}`, deliberately narrower
  than "any non-terminal": `parked` is an operator's deliberate pause, `submitted` and
  `pending-approval` have not been approved yet, and `routing-failed` is still being retried
  by the dispatcher. An `approved` parent is walked through `in-progress` first, so its
  history reads as claimed-then-closed rather than teleported.

  It is a fail-safe, not the primary path — agents still close their own tasks explicitly.
  Any failure inside it is logged at warning level and the submit returns normally.
- **Three task types: `docs`, `ticket_audit`, `ticket_audit_complete`.** All three were
  already documented in agent `CLAUDE.md` files and being called; every such `submit_task`
  failed validation here. `docs` is the writer's work-list type, introduced when
  `doc-update-queue.jsonl` was retired in favour of the queue.

### Changed
- **`list_tasks` rejects an unrecognised `status` instead of returning `[]`.** It used to
  accept anything and filter on it, which is how `writer/CLAUDE.md`'s
  `list_tasks(status="pending")` sweep — `pending` has never been a status here — returned
  an empty list for months, indistinguishable from "no work for you". An empty list is a
  legitimate answer to a well-formed question, so the only way to tell a typo apart from an
  empty queue is to refuse the typo. Raises `ValueError`; FastMCP surfaces the message and
  the valid vocabulary verbatim to the caller. Whitespace and a trailing comma are still
  tolerated.

  **Breaking for any caller passing a status outside `VALID_STATUSES`** — such a caller was
  already receiving nothing, so the change is from silent-empty to loud-error, not from
  working to broken.

## [0.5.0] - 2026-08-11

### Security
- **`update_task` ownership check.** `update_task_handler` now rejects any actor that is
  neither the task's `target_agent` nor `operator`. Previously any agent could transition
  any task, including one an operator had explicitly `parked` — closing an accepted-LOW
  finding from the `task-queue-park-amend-2026-08` audit. vikunja#325.
- **`VALID_TRANSITIONS["failed"]` is now an explicit literal set**, not derived from
  `NON_TERMINAL_STATUSES`. The derived form is how `parked` silently became a valid `failed`
  source when it was added — a future status addition can no longer widen this set without
  an explicit code change.

### Added
- **`routing-failed` admitted to `VALID_STATUSES`.** The dispatcher has always written this
  status on a failed dispatch attempt; it was never in the server's vocabulary, so an
  operator had no direct way to cancel or park a task stuck there — only the out-of-vocabulary
  repair path, with `allow_override=True` and a note. `routing-failed` is now a normal
  non-terminal status, reachable via the standard `cancelled` and `parked` operator
  transitions. It is deliberately **not** added to `VALID_TRANSITIONS["failed"]` — an agent
  must not be able to terminally fail a task the dispatcher is still retrying. vikunja#324.

## [0.4.0] - 2026-08-02

### Added
- **`parked` status** — pause a task without losing sight of it. Non-terminal, operator-only,
  reversible. New `park_task` / `unpark_task` tools and `POST /tasks/{id}/{park,unpark}`
  control routes. The prior status is recorded in `parked_from` and cleared on unpark, so
  `unpark_task` restores it without the caller having to know it; pass an explicit `status`
  to override, or to unpark a task that carries no marker.
- **Parked tasks are exempt from the TTL filter** in `list_tasks`. Parking is a deliberate
  bookmark — a parked task silently expiring out of the listing would defeat the point.
- **`amend_task`** — append-only corrections for queued tasks. Amendments accumulate under
  `payload.amendments` as `{timestamp, actor, reason, text}`; `payload.description` is never
  mutated. Permitted on any non-terminal task including `in-progress`, where the response
  sets `agent_may_have_started`. Only the task's `source_agent` or `operator` may amend —
  the target agent is rejected, so an agent cannot rewrite the instructions it was handed.
  Bounded at 10 amendments and 4096 chars each. Control route `POST /tasks/{id}/amend`.
- **`GET /queue/summary`** — counts by status across the active queue, behind the same
  shared-secret gate as the mutation routes. Statuses outside `VALID_STATUSES` are bucketed
  under `unknown` rather than dropped, so records from other direct-YAML writers stay visible.
- **Repair path for out-of-vocabulary statuses.** `set_task_status` now accepts a transition
  out of a status it does not recognise, given `allow_override=True` and a non-empty note.
  Two tasks on disk carried `complete` (not `completed`), written direct-to-YAML in May, and
  were unreachable by every mutation path — no tool could move them. The history entry
  records `repaired_from`. Only ever moves *out of* an invalid status; the target must be valid.

### Removed
- **`quarantine_task` / `restore_task`**, the `quarantine/` subdirectory, the
  `include_quarantined` loader parameter, and the `POST /tasks/{id}/{quarantine,restore}`
  routes. Superseded by `parked`. Quarantine moved a task's YAML into a subdirectory that no
  reader listed, so a quarantined task vanished from the only interface that showed it — and
  the confirm dialog promised a restore the UI never implemented. Making park a *status*
  dissolves that problem instead of requiring a second feature to patch it. Removed with no
  migration or compat shim: the mechanism was never used, `quarantine/` never existed on
  disk, no task ever carried `status: quarantined`, and no agent manifest granted either tool.
- **`alert_state`** is no longer initialised on task creation. The emitter was removed in
  July and nothing has read the block since. Existing task files keep theirs — the field is
  inert, and rewriting hundreds of YAMLs to strip a no-op buys nothing. This is deliberate,
  not an oversight; mutations preserve the residual block rather than silently dropping it.

### Fixed
- **`build-backend` was invalid.** `pyproject.toml` declared
  `setuptools.backends.legacy:build`, which is not a real backend, so both
  `pip install -e .` and `pip install .` failed with `BackendUnavailable` — including the
  `pip install -e ".[dev]"` that `AGENTS.md` documents as the test setup. Runtime was never
  affected (the image installs from `requirements.txt`), which is precisely why it went
  unnoticed: CI installed from `requirements.txt` too, so nothing ever built the package.
- README documented a **weaker port bind than production runs** — the compose example
  published `8485:8485` (all interfaces) while the same document's Security section states
  the port is loopback-limited and that any process with loopback access can mutate the queue
  without the secret. Copying it verbatim exposed an unauthenticated mutation endpoint to the
  LAN. Now `127.0.0.1:8485:8485`, with the reason stated inline.

### Changed
- CI installs via `pip install -e ".[dev]"` instead of `requirements.txt`, so packaging
  breakage fails CI in future. Python matrix narrowed to 3.11/3.12/3.13 and
  `requires-python` raised to `>=3.11`, aligning CI, `pyproject.toml`, the Dockerfile (3.12)
  and the fleet standard, which previously disagreed with each other three ways.
- Pinned `target-version = "py311"` for ruff and added the standard `tests/**` per-file
  ignores, closing the fleet ruff drift. This surfaced `UP017` (`datetime.UTC`), now applied.
- README sanitized for a public audience: hardcoded home paths, host-specific env-file
  locations and network names genericized. Documented the `parked`, `amend_task`, repair-path
  and `/queue/summary` behaviour, and added `amend_task` to the trust-model tool list — it is
  a new operator-mutating tool on the same unauthenticated loopback transport, and its
  source-agent check is an integrity control over a self-asserted actor, not authentication.

### Security
- Audited before merge (`task-queue-park-amend-2026-08`): **PASS — 0 Critical/High/Medium,
  2 Low, 10 Info**. Both Low findings confirmed and closed risks identified during the build
  rather than surfacing new ones, and neither required a code change. Both are now recorded
  as `SECURITY[accepted]` markers in `src/tools/queue.py`:
  - `unpark_task_handler` resolves its target status from a read taken outside the write
    lock. An illegal transition still cannot land — `set_task_status_handler` re-reads and
    re-validates under the lock — so the residual race is a redundant-but-valid transition
    plus a duplicate history entry, not a state-integrity bypass.
  - `parked` joining the derived `NON_TERMINAL_STATUSES` set automatically admitted it as a
    source for `update_task`'s `failed` transition, so an agent can fail a task the operator
    parked. The underlying gap — `update_task_handler` has no `target_agent` ownership check
    at all — is pre-existing. Tracked in vikunja#325.
- The `amend_task` authorization model was reviewed explicitly and confirmed sound: it is an
  integrity control over a self-asserted actor, not an authentication boundary, consistent
  with this server's documented unauthenticated-loopback trust model.

### Tests
- 122 tests, 91.9% coverage. New: park from each non-terminal status, park rejected from
  terminal, park unreachable via `update_task`, parked-past-TTL still listed, unpark
  round-trip and explicit-status unpark, unpark without a marker, parked task still
  cancellable; `amend_task` authorization (source accepted, operator accepted, target
  rejected, unrelated agent rejected), append-not-replace, both bounds, adversarial YAML in
  amendment text, terminal/archived rejection; repair path with and without override/note,
  `routing-failed` repair, and rejection of an invalid repair *target*; `/queue/summary`
  including the `unknown` bucket; and 404s proving the retired quarantine routes are gone.

## [0.3.1] - 2026-07-20

### Changed
- `set_task_status`'s rejection error now points at `update_task` when the
  rejected `(current → target)` transition is one `update_task` accepts
  (`approved→in-progress`, `in-progress→completed`, or `→failed` from any
  non-terminal). `set_task_status` is operator-only and structurally cannot
  reach terminal statuses even with `allow_override`; the forward
  `in-progress→completed` path lives on `update_task`. Message-only — no change
  to `VALID_TRANSITIONS`/`OPERATOR_TRANSITIONS` semantics.

## [0.3.0] - 2026-06-25

### Added
- Task-dismissal lifecycle: `cancelled` terminal status; `set_task_status` (operator transitions — approve, cancel, or advance a missed task via an audited `allow_override`); `cancel_task`; `quarantine_task` / `restore_task` (move a task's YAML to/from `quarantine/`, recoverable, no hard-delete). Four new MCP tools.
- Shared-secret HTTP control API mounted as FastMCP custom routes on the existing port 8485: `POST /tasks/{id}/{approve,cancel,status,quarantine,restore}`. Delegates to the lifecycle handlers (transition validation + `fcntl` locking + atomic writes), gated by an `X-Task-Queue-Secret` header (constant-time compare, fails closed). The single validated mutation path for the CloudCLI plugin and Matrix bot — ends the prior three-writer divergence.
- `workflow_mode` field on task schema (`semi-auto` | `auto`, default `semi-auto`). Controls whether the dispatcher auto-launches the target agent headlessly (`auto`) or queues for operator pickup with a Matrix room notification (`semi-auto`).
- `VALID_WORKFLOW_MODES` constant; `workflow_mode` validated at submission, stored as top-level task field, returned by `get_task` and `list_tasks`.

### Changed
- `get_task` now also resolves quarantined tasks; `list_tasks` excludes them (mirrors `archive/`).
- Baseline repo polish: ruff lint + format config, coverage gate (`fail_under = 80`), CI actions SHA-pinned with `ruff check` / `ruff format --check` / `pytest --cov` steps, README badges and docs.

### Security
- Control-API secret comparison uses byte operands, so a non-ASCII `X-Task-Queue-Secret` header yields a clean 401 instead of a 500 (audit L-02).
- Documented the loopback trust model in the README — the shared secret gates only the cross-process HTTP control routes; the operator-mutating MCP tools remain reachable via the unauthenticated loopback `/mcp/` endpoint by design (audit L-01).

### Tests
- Full coverage of the lifecycle handlers, the previously-untested `server.py` tool wiring, and the control API (including the secret gate: missing / wrong / non-ASCII / unconfigured → 401). 82 tests, 90.7% coverage.

## [0.2.0] - 2026-05-28

### Added
- `VALID_TASK_TYPES` constant defining the allowed task type values: `build`, `deploy`, `fix`, `research`, `review`, `audit`, `notify`.
- Validation of `task_type` in `submit_task_handler` — returns an error dict for unknown types.
- Blank-string guards for `source_agent`, `target_agent`, and `summary` at submission time.
- 8 new tests covering validation edge cases: blank fields, invalid task_type, all valid task types, archived task update, output=None preservation, TTL boundary values.

### Fixed
- `update_task_handler` now searches the archive directory — previously returned "not found" for archived tasks; now returns a clear "task is archived" error.
