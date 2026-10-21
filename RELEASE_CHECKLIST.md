# v4.0.1 release checks

- [x] Workspace/tab isolation, file no-clobber, queue/ownership, text fallback,
  idempotency/conflict handling and empty-file regressions covered.
- [x] Core Windows Python suite: 135 tests, 134 pass and one POSIX-only skip.
- [x] Core Linux Python suite: all 135 tests pass, including the detached runner.
- [x] Node runtime: three behavior tests pass.
- [x] Live browser workspace: file roundtrips, interrupted resume, bounded logs,
  detached jobs, idempotency, artifacts, timeout recovery and owned-job cancellation pass.
- [ ] Hosted GitHub CI: inspect the run on the published commit.
- [ ] Other controller platforms and large-transfer performance remain unverified.

Detailed scope: `docs/VALIDATION_v4.0.1.md`.
