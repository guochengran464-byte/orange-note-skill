# Compatibility

v4.1.4 keeps independent conversation pages within a five-minute idle window.
Its current validation scope is recorded in `docs/VALIDATION_v4.1.4.md`.
The three-agent results below describe the earlier v4.1.0 release.

Orange Note provides a reusable Agent operation interface, with a built-in
connector for supported browser-hosted Jupyter workspaces. The current URL
validator accepts that connector's workspace format. Arbitrary sites or
self-hosted Jupyter servers require a connector adaptation.

| Component | Status |
| --- | --- |
| Windows native Python controller | Tested |
| Edge with BrowserSkill | Tested |
| BrowserSkill CLI 0.3.2 / Edge extension 0.3.3 | Used for live verification |
| Linux workspace with Jupyter REST/WebSocket APIs | Tested through the built-in connector |
| macOS/Linux/WSL as the local controller | Unverified |
| Other browser profiles, new CLI/extension versions | Re-run the smoke checks before relying on them |

The connector uses `bsk session`, `tab`, `navigate` and main-frame `evaluate`.
Browser state and sessions are isolated by workspace. A controller-owned Jupyter
kernel handles execution and atomic writes; file APIs handle small reads and ranged downloads.
v4.1.0 adds explicit conversation sessions on top of that engine. Focused local
checks cover session management and Windows file locking. Three Luna agents
also passed live isolation, persistence, overlapping execution and independent
cleanup with BrowserSkill CLI/extension 0.3.3 and Edge 155.0.0.0.
POSIX controller locking and heavy-load capacity remain unverified for this release.

Each user supplies their own supported workspace URL. Project/workspace IDs and
browser state are stored in local user/session config, never in shipped defaults.

Pages are reused between operations and expire after about five idle minutes.
The documented launch uses `--session-idle 5m`; a busy session does not renew an
independent idle session. Browser/daemon failure, login expiry and server culling
can interrupt earlier. A temporary failed probe keeps the existing page open.

The default server paths are `/data/work` and `/data/work/agent_jobs`. A different
server layout needs corresponding connector/path adaptation; changing a label
alone does not establish compatibility.

See `docs/VALIDATION_v4.1.0.md` for this release and `docs/VALIDATION_v4.0.1.md`
for historical engine checks. If connection checks fail,
report the actual failed layer and sanitized CLI output.
