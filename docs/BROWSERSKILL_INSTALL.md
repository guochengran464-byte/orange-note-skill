# BrowserSkill setup

Orange Note uses the installed BrowserSkill CLI and the user's existing browser
login. It does not install or upgrade BrowserSkill automatically.

1. Install the CLI and browser extension using the
   [BrowserSkill project instructions](https://github.com/Tencent/BrowserSkill).
2. Enable the extension's local connection in the same profile as the target workspace.
3. Check the CLI and connection:

```bash
bsk --version
bsk doctor
python scripts/sn.py doctor
```

If the executable is outside PATH, set `BSK_BIN` to its full path. The tested
combination used Windows, Edge, CLI 0.3.2 and extension 0.3.3.

In a host that cannot start a detached Windows daemon, use its managed background
task facility to run:

```bash
bsk daemon start --foreground --session-idle 5m
```

Keep this managed foreground task alive between Agent calls. In an independent
terminal that supports detached daemons, use the same `--session-idle 5m`
flag without `--foreground`. Reuse a reachable shared daemon and verify
`bsk status --json` from another tool call. Reconfiguring idle retention requires
a daemon restart: do it only with no active sessions, preserving other tasks.

BrowserSkill 0.3.3 defaults to five-minute session-idle expiry; make it explicit
with `5m`. The setting is daemon-wide, but idle activity is tracked per session.
Keep the page between operations and let it expire after about five idle minutes.
Do not poll solely to keep it alive. This is not a total-lifetime cap, and expiry
does not stop the whole DCS workspace or intentionally cancel detached jobs.
The remote Jupyter server determines eventual cleanup of abandoned kernels.
Normal `doctor` output does not verify the daemon's idle launch argument; retain
the startup command in the host's launch/handoff record.

Source reference: [BrowserSkill daemon arguments](https://github.com/Tencent/BrowserSkill/blob/main/crates/bsk-cli/src/cli/daemon.rs).

Open and log into the supported target workspace yourself. When it is ready:

```bash
python scripts/sn.py session create --url "<your supported workspace URL>"
python scripts/sn.py --session <returned-logical-id> probe
```

The controller operates its own Agent Window. A reaped session may be rebuilt
from the saved workspace. An expired login needs attention in the original
browser profile. Use `doctor --deep` to distinguish the failed layer.

Use your own URL; IDs are parsed from it and stored in your local session config.
Keep the returned logical ID for this conversation and pass it on each command.
Task completion leaves that controller's page open for reuse within the idle
window. After expiry, reconnect using the same logical ID when work is requested;
in-memory Python variables may be lost. An explicit user release request can run
`python scripts/sn.py --session <id> close` immediately, cleaning up its recorded
kernel and page. Other conversations have their own activity and idle deadlines.
