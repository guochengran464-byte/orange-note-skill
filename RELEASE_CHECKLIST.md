# v4.1.0 release checks

This release uses focused checks only. See `docs/VALIDATION_v4.1.0.md`.
- [x] Focused local session checks and skill metadata validation.
- [x] Three live Luna agents: separate sessions/tabs/kernels and retained variables.
- [x] Overlapping execution and independent session closure.
- [x] Public configuration contains no personal workspace IDs or saved browser state.
- [ ] Heavy-load testing and a full local regression run were not requested.

## Historical v4.0.1 checks

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
