# macOS validation — 2026-11-09

Local adaptation of upstream v4.2.0 (release archive SHA-256
`5c9f3fe0bb7f189f4a9a2e0507bb8754a733cbc00ee1ae462b1924e647e69685`).

Environment: macOS 27.0, Apple Silicon, Python 3.12.14; BrowserSkill CLI,
daemon and Edge extension 0.4.2, protocol 1.8; Edge 157.

Changes: macOS/POSIX interpreter launcher; observation receipts for guarded
evaluation/navigation on BrowserSkill 0.4.1+; explicit browser instance binding
persisted per logical session; platform/lock backend reported by doctor.
Existing Windows behavior and BrowserSkill 0.3/0.4.0 paths remain covered by tests.

Verified live:

- Native daemon reachable across independent shell tool calls, launched with
  `--session-idle 5m`; all BrowserSkill doctor checks passed.
- Edge popup showed connected CLI/extension 0.4.2.
- Example Domain opened/read through BrowserSkill and the adapted Orange Note
  evaluation wrapper; the test session was explicitly stopped.
- User-selected DCS workspace connected through a separate controller-owned page.
  Jupyter status, root and work endpoint probes returned HTTP 200.
- A controller-owned Python kernel executed a bounded calculation successfully:
  `2 + 3 = 5`; remote Python 3.10.0, Linux; no stderr or timeout.
- Workspace credentials remained in the browser. No notebooks or project files
  were edited by the connection smoke check.

The Python suite has 182 cases: 181 passed, one remote Linux launcher integration
case skipped because this Mac does not have `setsid`. The JavaScript suite passed
all 13 cases. Skill frontmatter validation passed.

Not live-tested: file transfers, long-running jobs, network interruption, login
expiry and multiple simultaneous remote kernels. Remote background commands still
run on Linux; macOS controller validation does not imply a macOS remote server.
