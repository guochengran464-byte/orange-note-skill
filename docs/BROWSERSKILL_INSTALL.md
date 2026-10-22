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
task facility to run `bsk daemon start --foreground`. Reuse a reachable shared
daemon and verify `bsk status --json` from another tool call. Avoid restarting
a daemon that other tasks are using.

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
When done, `python scripts/sn.py --session <id> close` cleans up that controller's
recorded kernel and browser session. Other conversations use their own logical IDs.
