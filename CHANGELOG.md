# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.2.0] - 2026-05-28

### Added
- `VALID_TASK_TYPES` constant defining the allowed task type values: `build`, `deploy`, `fix`, `research`, `review`, `audit`, `notify`.
- Validation of `task_type` in `submit_task_handler` — returns an error dict for unknown types.
- Blank-string guards for `source_agent`, `target_agent`, and `summary` at submission time.
- 8 new tests covering validation edge cases: blank fields, invalid task_type, all valid task types, archived task update, output=None preservation, TTL boundary values.

### Fixed
- `update_task_handler` now searches the archive directory — previously returned "not found" for archived tasks; now returns a clear "task is archived" error.
