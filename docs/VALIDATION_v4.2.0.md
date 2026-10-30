# v4.2.0 validation

Date: 2026-10-30 (Asia/Shanghai). This file describes the checks performed
before publication; it does not claim that every browser or server is supported.

## BrowserSkill compatibility

- Windows Python 3.14; Edge; BrowserSkill CLI, daemon and extension 0.4.0.
- In a user-selected workspace, newly created independent sessions connected
  and deep doctor reached Jupyter status 200. Two sessions executed concurrently,
  retained different kernel variables, and reused their own kernels.
- A 512 KiB normal file and a symbolic link to it downloaded with matching
  SHA-256. A link to an empty file also downloaded with matching SHA-256.
  The server exposed target sizes through HTTP Content-Range; servers without
  that header have not been validated for symbolic-link correction.
- A 12 KiB binary upload/download matched SHA-256; a small text write/read passed.
- A short detached job finished with exit zero, and repeating its idempotency
  key returned the original job without redispatch.
- BrowserSkill 0.3 behavior is covered by simulated CLI regression tests;
  0.3 was not re-tested live for this release. Heavy load, many-agent overlap,
  active human intervention and real network loss were not live-tested.

## Safety and limits

Coordination refusal blocks the action, and unknown outcomes are not replayed.
Do not replace a running controller or shared daemon while an agent operation
is active. The existing logical session configuration and job receipts are not
migrated or deleted by this release. Page expiry does not cancel detached jobs.

Symbolic-link correction requires the HTTP range total to differ from Contents
metadata. When that mismatch is detected, a controller-owned kernel validates
the symlink and tracks both link and target metadata. If a server hides range
totals, or reports the same size for a link and its target, this correction is
not guaranteed. Default SHA-256 verification for files up to 64 MiB still
detects a content mismatch; larger downloads report size-only verification.

See the automated test output and CI for current platform-specific test counts.
