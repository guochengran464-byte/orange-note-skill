# v4.1.4 release checks

See `docs/VALIDATION_v4.1.4.md` for current scope. Historical live checks are
identified separately from local regressions and the configured idle timeout.
- [x] Full Windows Python regression suite and added behavioral safety checks.
- [x] Node syntax and runtime behavior checks, including repeated execution and recovery.
- [x] Skill metadata validation, CLI help, compilation and diff whitespace checks.
- [x] Same skill identity, existing command names, config format and invocation policy.
- [x] Package excludes Git metadata, local state, receipts, caches and private workspace IDs.
- [x] Live DCS roundtrips, hard-link publication and atomic replacement on the actual shared mount.
- [x] Independent pages, repeated connect and overlapping execution in a live workspace.
- [x] Five-minute daemon launch argument and consistent idle-window instructions.
- [ ] Fresh full five-minute expiry trial; not repeated for the final setting.
- [ ] Large-data and long-running/heavy-load checks; outside this release's validation.

Merge only after the repository's Windows/Linux CI matrix is green. The existing
main-branch workflow creates the versioned release and archive after merge.

Before upgrading an installed copy, finish in-flight calls and keep session state
and detached jobs outside the skill directory. A required browser/kernel restart
must fit the user's explicit release request; upgrades do not authorize closing
another agent's page. An upstream push to main may trigger
the existing automatic release workflow; review that publishing decision first.

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
