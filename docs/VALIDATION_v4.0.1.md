# v4.0.1 validation

Date: 2026-10-21 (Asia/Shanghai).

## Core tests

The corrected core passed 135 Python tests on Linux, including an actual
bash/setsid detached runner. Windows passed 134 of those tests and skipped the
POSIX-only runner case. Three Node behavior tests covered runtime reinjection,
queue serialization, tab ownership and explicit cleanup.

Added regressions cover concurrent workspace switches, isolated browser sessions,
workspace-scoped idempotency, changed-command/cwd conflicts, simultaneous same-key
submissions, bounded text fallback, preserved authentication errors, atomic binary
creation/readback, default download no-clobber, empty files and unscoped partials.

## Live browser-workspace checks

Using an independent test configuration and unique test directory:

- Layered diagnostics and connection succeeded.
- Chinese-named PNG, 300 KiB arbitrary binary and empty-file transfers matched
  exact content and remote SHA-256; default overwrite attempts failed.
- After an interrupted read, the completed 256 KiB partial resumed and verified.
- A 45,000-byte log returned a bounded 16,384-byte preview.
- A detached job finished with exit code 0 and artifact metadata. Reusing its
  key under another display name returned the same job; a changed command was rejected.
- A one-second execution timeout was followed by successful kernel recovery.
- A controller-owned sleep job was cancelled and finished with exit code 143.

The public packaging changes the skill name, metadata and documentation. It
retains the tested execution engine and its existing connector validation.

## Limits

These checks establish operation in a supported Jupyter workspace. They do not
establish support for arbitrary websites, self-hosted notebook servers or every
browser profile. New connectors need their own verification.

Uploads remain capped at 4 MiB. Downloads over 64 MiB use the documented size-only
default policy. Large-transfer performance and login/connection loss across all
environments were not tested. Default local publication requires hard-link support
in the destination directory and fails safely if unavailable.

Kernel ownership lives in tab-scoped sessionStorage. Closing a tab outside the
controller's `close` command may leave an owned kernel for server-side cleanup.
Production research files and existing jobs were not modified during testing.
