# Compatibility

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

The default server paths are `/data/work` and `/data/work/agent_jobs`. A different
server layout needs corresponding connector/path adaptation; changing a label
alone does not establish compatibility.

See `docs/VALIDATION_v4.0.1.md` for completed checks. If connection checks fail,
report the actual failed layer and sanitized CLI output.
