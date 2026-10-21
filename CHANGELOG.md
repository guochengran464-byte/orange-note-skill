# Changelog

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
