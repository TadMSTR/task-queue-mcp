# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
