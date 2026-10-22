# v4.1.0 first usable session version

Date: 2026-10-22 (Asia/Shanghai).

## Delivered

- `session create --url URL` allocates a persistent logical ID and connects.
  Connection errors return that ID for later `--session ID connect` recovery.
- `session list` reads saved records without accessing the browser.
- `--session ID` selects separate config, browser state and job receipts under
  the existing config root's `sessions/ID` directory. Each session creates its
  own BrowserSkill session and controller page; the existing runtime manages
  page-specific kernel ownership.
- Commands in one logical session hold an OS file lock for their full duration.
  Another process waits up to 10 seconds, then returns `session_busy`.
- A selected session is bound to one workspace. Conflicting `--url` and `SN_URL`
  values are rejected before browser access. The BrowserSkill session ID is
  pinned during each command, including chunked payload calls.
- `close` only cleans the selected controller and retains configuration,
  receipts and detached jobs. Reconnect can rebuild its browser session.
- Legacy commands without `--session` retain their shared-state behavior.

## Focused checks only

Per the user's request, this version did not run the full regression suite.

```text
python -B -m unittest discover -s tests -p test_sessions.py -v
4 tests passed on Windows in approximately 0.3 seconds, including different
users' independent project/workspace URLs.
```

The checks cover two same-workspace conversations with different browser IDs,
tabs and directories; command routing; closing A while retaining B; restoring B
after its browser session disappears; target/ID rejection; retaining an ID on
connection failure; and actual cross-process lock exclusion and release.

Browser calls in the routing check are simulated. This proves local selection
and lifecycle behavior, not live browser/Jupyter isolation or parallel throughput.
The lock check uses a real child Python process and the Windows OS file lock.

Skill metadata validation passed using Python UTF-8 mode; CLI help also ran.
No new Python dependencies were added. The JavaScript runtime was not changed.

## Live three-agent check

Three gpt-6-luna agents connected to the same user-selected workspace through
independent logical sessions. Each obtained a different BrowserSkill session,
control tab and Jupyter kernel process. Each set a different Python marker and
reread it successfully in a second CLI invocation using the same kernel.

Two subsequent four-second cells had approximately 3.643 seconds of measured
execution overlap. While those requests were in flight, the third session was
closed. Both requests completed successfully with their original markers and
kernel PIDs, without timeout or kernel loss.

All three test controllers were closed afterward; each recorded kernel deletion
returned HTTP 204. BrowserSkill then reported zero active sessions. No research
files or background jobs were created or changed for this check.

Environment: Windows Python 3.14.4, BrowserSkill CLI/daemon 0.3.3, Edge 155.0.0.0,
extension 0.3.3, protocol 1.3. No version skew was reported.
Personal workspace URLs, IDs and local browser state are omitted from this public record.

## Remaining limits

- The live check was lightweight; heavy load, long jobs and every browser profile
  were not tested. BrowserSkill's upstream global scheduling was not audited.
- POSIX locking and the full existing regression suite were not run this round.
- Browser login, server files, permissions and compute resources remain shared.
  Give independent tasks different output paths and include the logical ID in
  independent idempotency keys. `jobs --remote` lists workspace-wide jobs.
- Workspace-scoped idempotency is unchanged. Reusing a key from another receipt
  directory may report an existing remote job directory; inspect the returned
  job ID instead of blindly submitting again.
- A lost browser page can lose its kernel ownership record and Python variables.
  Cleanup does not guess which unrecorded server kernels belong to the session.
- Connection config remains per user/session. The public package has no preset
  project/workspace target; the current connector still requires supported DCS URLs.

Historical engine validation remains in `VALIDATION_v4.0.1.md`; it is not a claim
that the new v4.1.0 behavior has passed those older live checks.
