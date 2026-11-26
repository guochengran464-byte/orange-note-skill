# macOS controller

This local adaptation retains the v4.2.0 remote Linux/Jupyter engine.

Use `scripts/sn doctor` and `scripts/sn session create --url "<workspace URL>"`.
The launcher selects Codex's bundled Python or Homebrew Python. Override it with
`ORANGE_NOTE_PYTHON=/absolute/path/to/python3`; `/usr/bin/python3` may be an Apple
developer-tools placeholder and is not selected automatically on macOS.

Install the native Apple Silicon or Intel BrowserSkill CLI in `~/.local/bin/bsk`.
CLI, daemon and Edge extension 0.4.2 have been connected on this Apple Silicon
Mac. Start a missing daemon with `~/.local/bin/bsk daemon start --session-idle 5m`
and verify with `~/.local/bin/bsk status --json` from a separate command.
Reuse a running daemon. Normal idle exit is expected; later CLI calls can start it.

BrowserSkill 0.4.1+ requires the top-level `observation_id` from a fresh sync for
script evaluation and navigation of an existing page. The adapter passes that receipt and fails before dispatch if
it is missing. It never substitutes the nested ref-map observation ID.

To bind new Orange Note sessions to a verified Edge instance, set `BSK_BROWSER`
on each call, or add `bsk_browser_id` to the applicable local config. Obtain the
instance from the extension popup and verify it with `bsk browsers --json`.
Session configs are under `~/.config/stereonote` unless `SN_CONFIG_DIR` overrides
the location. Keep profile IDs and workspace IDs out of published packages.

Controller locking uses macOS `fcntl.flock`. The remote job launcher still runs
on Linux and requires `setsid`; its local integration test skips hosts without
that command. This does not mean remote background execution was verified on Mac.
Live validation on this Mac also connected to the user-selected DCS workspace;
see [the validation record](VALIDATION_MACOS.md) for the exact verified scope.
