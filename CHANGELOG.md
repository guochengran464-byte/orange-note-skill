# Changelog

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
