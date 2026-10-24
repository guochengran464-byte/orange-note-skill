# Changelog

## 4.1.4 — 2026-10-24

### Session lifecycle

- Reuse each agent's independent page and healthy kernel between operations;
  completing a task no longer implies closing its page. Configure BrowserSkill
  with `--session-idle 5m`: expire each session after about five idle minutes,
  rather than imposing a five-minute total lifetime. Explicit release still
  closes only the selected session. Reconnect with its saved logical ID on demand.
- Repeated connect, including the same supplied URL, keeps the existing page.
  Prefer its saved tab ID; failed probes or unverified session listings report
  an error instead of refreshing a page or creating a duplicate session.

### Reliability and data protection

- Stage and verify text/binary writes before atomic publication or authorized
  replacement, preserving existing files on write/readback failure.
- Close each execution WebSocket while retaining healthy kernels and queues.
  Ignore unrelated idle messages and recover after execution timeout/channel loss.
- Report uncertain navigation errors without replaying executable expressions.
- Read RDS input in a fresh job each time, avoiding stale path-only cached results
  and conflicts with removed jobs or another session's inspection.
- Add `prune --days N [--dry-run]`. Require exact job IDs, matching manifests,
  verified old completion and absent PID/process groups; preserve unknown,
  malformed, active and unverifiable records. Report deletion errors honestly.
- Route `.txt` inspection through the bounded text reader. CSV/TSV inspection
  uses file size instead of counting lines across the entire source.
- Buffer upload chunks in an array and join once; keep runtime/cache versions
  aligned. Reject ignored Range responses before reading an oversized body.
- Include `work_writable` in deep diagnostics and expand behavioral regressions.

### Compatibility notes

- Skill identity, existing command names, session configuration and general
  submission idempotency remain compatible; no Python dependency is added.
- `prune` is workspace-wide and includes task logs/artifacts. `eligible` counts
  candidates, `removed` counts actual deletions (zero for dry-run), and unmeasured
  `freed_bytes` is `null`. Browser idle expiry does not cancel detached jobs or
  guarantee that Jupyter has reclaimed remote kernels.

## 4.1.0 — 2026-10-22

- Add `session create --url`, `session list` and explicit `--session ID` selection.
- Isolate each conversation's config, browser session/tab, kernel and receipts.
- Serialize commands per logical session with an OS file lock, leaving other sessions independent.
- Bind the workspace and browser session for each command; close/reconnect retain the logical ID and detached jobs.
- Preserve commands without `--session` as the legacy shared-state interface.
- Derive workspace identity from each user's supplied URL; publish no personal workspace configuration.
- Verify three Luna agents in one live workspace, including independent kernels, retained variables,
  overlapping execution and closing one session while the other two continue.
- Keep validation focused; heavy-load and long-job stress tests were not performed.

## 4.0.1 — 2026-10-21

Orange Note public package: a reusable Agent operation skill for supported
browser-hosted Jupyter workspaces. Includes file inspection/transfer, bounded
execution, persistent background jobs and layered diagnostics.

- Bind cached tabs and sessions to the selected workspace, including concurrent target switches.
- Scope idempotency keys to the workspace; preserve identity across display-name changes.
- Reject command/cwd conflicts and reserve local receipts atomically before dispatch.
- Share exclusive creation and readback between text writes and binary uploads.
- Preserve existing local files during default download publication and support empty files.
- Bind resumable partials to the originating workspace and source identity.
- Preserve the runtime queue across reinjection and isolate kernel ownership by page/workspace.
- Give chunked payloads independent browser buffers.
- Let oversized text reach the bounded-reader fallback without hiding authentication errors.
- Run Python regression tests and real Node runtime behavior tests in CI.

The public package uses Orange Note naming. The existing CLI filename and
runtime configuration compatibility are retained for existing automation.
