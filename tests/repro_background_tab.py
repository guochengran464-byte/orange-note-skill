"""Live reproduction for Orange Note failures while its Agent tab is hidden."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def run_json(command, timeout=70):
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "invalid_json",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-1000:],
            "stderr": completed.stderr[-1000:],
        }
    if completed.returncode and not isinstance(payload, dict):
        return {"ok": False, "error": "command_failed", "payload": payload}
    return payload


def bsk_json(browser_session, *args):
    return run_json(["bsk", *args, "--session", browser_session, "--json"])


def page_state(browser_session, tab_id):
    sync = bsk_json(
        browser_session, "cooperate", "sync", "--tab-id", str(tab_id)
    )
    context = sync.get("context_id")
    if not context:
        return {"ok": False, "stage": "sync", "error": sync}
    state = bsk_json(
        browser_session,
        "evaluate",
        "--tab-id",
        str(tab_id),
        "--context",
        context,
        '({readyState:document.readyState,visibility:document.visibilityState,'
        'url:location.href,now:performance.now(),'
        'splash:Boolean(document.querySelector("#jupyterlab-splash"))})',
    )
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-session", required=True)
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--startup-samples", type=int, default=0)
    args = parser.parse_args()

    failures = []
    previous_now = None
    previous_url = None
    for sample in range(args.samples):
        if sample:
            time.sleep(args.interval)
        tabs = bsk_json(args.browser_session, "tab", "list", "--scope", "agent")
        tab = next(
            (item for item in tabs.get("tabs", []) if item.get("tab_id") == args.tab_id),
            None,
        )
        state = page_state(args.browser_session, args.tab_id)
        value = state.get("value", {})
        record = {
            "sample": sample,
            "tab_active": tab.get("active") if tab else None,
            "page": value,
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)

        if not tab:
            failures.append(f"sample {sample}: target tab is missing")
        if not state.get("ok"):
            failures.append(f"sample {sample}: page evaluation failed")
        if value.get("visibility") != "hidden":
            failures.append(f"sample {sample}: page is not hidden")
        if sample >= args.startup_samples:
            if value.get("readyState") != "complete" or value.get("splash"):
                failures.append(f"sample {sample}: page is reloading or stuck loading")
            if previous_now is not None and value.get("now", 0) <= previous_now:
                failures.append(f"sample {sample}: page navigation reset performance.now")
            if previous_url is not None and value.get("url") != previous_url:
                failures.append(f"sample {sample}: page URL changed")
        previous_now = value.get("now", previous_now)
        previous_url = value.get("url", previous_url)

    print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
