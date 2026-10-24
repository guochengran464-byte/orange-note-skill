# v4.1.4 validation

Date: 2026-10-24 (Asia/Shanghai).

## Local regression checks

Windows Python 3.14: 166 tests, 165 passed and one POSIX-only detached-runner
integration test skipped. Node: all 11 runtime behavior tests passed. Python
compilation, JavaScript syntax, CLI help, skill metadata and diff checks passed.
The repository CI runs the suite on Windows/Linux with Python 3.9 and 3.12 plus Node 22.

Behavioral coverage includes conservative cleanup with real disposable local
directories, malformed records and symlinks; PID/process-group uncertainty;
deletion errors; changed RDS data; two independent sessions; inspection after
an old remote job is removed; general submission idempotency; uncertain dispatch;
atomic file publication, failed overwrite and a competing writer; connection
closure, runtime reinjection, queue preservation and download range boundaries.

## Live DCS checks

Environment: Windows, Edge 155.0.0.0, BrowserSkill CLI/daemon/extension 0.3.3 and
a user-selected DCS StereoNote workspace. Private URLs, IDs and local state are
omitted. Only small disposable files and test jobs were used.

- Independent sessions had distinct pages and kernels, retained separate Python
  markers and ran two-second cells with about 1.575 seconds of overlap.
- Repeated connect preserved tab identity, page timeOrigin, kernel PID and markers.
- Chinese text creation, default no-clobber and authorized replacement passed.
  A 307,212-byte binary upload/ranged download matched both bytes and remote SHA-256.
- Injected write failure on the shared mount preserved the original destination.
- Real Rscript/jsonlite inspections of an updated same-path RDS returned lengths
  one and two through two separate jobs.
- The actual cleanup program removed only one eligible disposable Linux fixture;
  running, malformed and unrelated fixtures stayed. The shared job root was tested
  only with dry-run, reporting zero deletions.
- Python error preserved a healthy kernel; execution timeout replaced only its own
  lost kernel while another session retained its variables.
- A detached test job survived session closure, completed with exit zero and had
  the expected artifact SHA-256. Same-key submission reused the job; changed commands
  produced a conflict. All three real background test jobs finished with exit zero.
- Five consecutive executions reused a kernel; its server metadata reported zero
  remaining communication channels after execution.

## Final five-minute setting

The final BrowserSkill daemon command line was read back as
`--foreground --session-idle 5m`. Two disposable test pages were released only
after more than 300 idle seconds, before changing that daemon's setting; no
unrelated sessions were present. No keepalive process or application-side timer
was added. Logical IDs and detached-job receipts remain available after expiry.

A separate long-retention experiment left two pages idle for 335 seconds with
stable identities. It is evidence of page reuse/isolation, not evidence that the
final five-minute setting has completed an automatic-expiry trial. A fresh full
five-minute expiry trial was not repeated for the final setting.

## Limits and observed failures

- Initial page loading occasionally reported `Inspected target navigated or closed`.
  Executable expressions were not blindly replayed. Test recovery was restricted to
  known read-only checks or idempotent in-memory test-marker assignments.
- The final close responses contained `kernels: null`, so remote kernel deletion
  was not confirmed. Jupyter controls eventual cleanup of abandoned kernels;
  browser expiry is not a DCS workspace shutdown or detached-job cancellation.
- Large objects, long-running research workloads, heavy concurrency, browser/OS
  restart and authentication renewal were not tested. RDS still loads the whole
  remote object in memory. Server files and compute are shared between sessions.
