# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

To report a vulnerability, use one of these channels:

- **GitHub private disclosure:** Use the [Security tab](https://github.com/TadMSTR/task-queue-mcp/security/advisories/new) to submit a private advisory.
- **Email:** Send a description to `security.i9v75@8alias.com` with the subject line `[task-queue-mcp] Security Report`.

Include as much detail as possible: the affected component, steps to reproduce, and potential impact.

## Scope

**In scope:**

- Task injection enabling unauthorized task creation, modification, or status manipulation
- Privilege escalation by dispatching tasks to unintended agents with elevated access
- Information disclosure through task payload content containing credentials or sensitive data
- Dependency vulnerabilities with a plausible exploitation path in task-queue-mcp's usage

**Out of scope:**

- Vulnerabilities in the host system, underlying services, or MCP transport layer
- Issues that require attacker control of configuration environment variables
  (operator-controlled trust boundaries, not input attack surfaces)
- Theoretical weaknesses without a realistic attack path against the MCP tool surface

## Authentication model

Port 8485 serves two surfaces with separate credentials:

- **`/mcp` (agents):** a per-agent bearer token in `Authorization`. It authenticates the
  caller and names it; `actor` is derived from it.
- **`/tasks…` and `/queue/summary` (operator clients):** a per-client token in
  `X-Task-Queue-Token`, with a `read` or `operator-write` scope required per route. The
  server stores only `sha256:` digests of client tokens. An agent bearer grants nothing on
  these routes, and a client token grants nothing on `/mcp`.

These contain a mistaken or prompt-injected agent acting through its own tool surface. They
are not a boundary against a process running as the same OS user that owns the token files.
A client token is only a boundary when its plaintext is held by a different OS user from the
agents. See the README's Trust model section.

Reports that assume the attacker can read the operator's env or token files as the owning
user are out of scope under "attacker control of configuration environment variables" below.

## Response Expectations

| Stage | Timeline |
|-------|----------|
| Acknowledgement | Within 3 business days |
| Initial assessment | Within 7 business days |
| Fix or remediation plan | Within 30 days for critical/high; 60 days for medium/low |

This is a personal project maintained by one developer. Response times are best-effort.
If you haven't heard back within 3 business days, a follow-up email is welcome.

## Disclosure

Coordinated disclosure is preferred. Please allow time for a fix to be released before
public disclosure. The CHANGELOG documents remediated findings at an appropriate level
of detail after each release.
