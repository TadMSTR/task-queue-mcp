# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- `workflow_mode` field on task schema (`semi-auto` | `auto`, default `semi-auto`). Controls whether the dispatcher auto-launches the target agent headlessly (`auto`) or queues for operator pickup with a Matrix room notification (`semi-auto`).
- `VALID_WORKFLOW_MODES` constant; `workflow_mode` validated at submission, stored as top-level task field, returned by `get_task` and `list_tasks`.
- 6 new tests covering `workflow_mode` validation, defaults, and list/get return behavior.

## [0.2.0] - 2026-05-28

### Added
- `VALID_TASK_TYPES` constant defining the allowed task type values: `build`, `deploy`, `fix`, `research`, `review`, `audit`, `notify`.
- Validation of `task_type` in `submit_task_handler` — returns an error dict for unknown types.
- Blank-string guards for `source_agent`, `target_agent`, and `summary` at submission time.
- 8 new tests covering validation edge cases: blank fields, invalid task_type, all valid task types, archived task update, output=None preservation, TTL boundary values.

### Fixed
- `update_task_handler` now searches the archive directory — previously returned "not found" for archived tasks; now returns a clear "task is archived" error.
