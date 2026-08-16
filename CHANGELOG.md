# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
