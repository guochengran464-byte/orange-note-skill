import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sn.py"
spec = importlib.util.spec_from_file_location("sn_sessions", SCRIPT)
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)
URL = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"SN_CONFIG_DIR": self.tmp.name, "SN_URL": ""}))

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sn.main(list(args))
        return rc, json.loads(out.getvalue())

    def test_two_conversations_close_and_recover_independently(self):
        live, calls, navigations = {}, [], []
        sequence = iter(range(1, 20))

        def bsk(*args, session=None, tab_id=None, **kwargs):
            if args == ("session", "start"):
                number = next(sequence)
                sid = "browser-" + str(number)
                live[sid] = {"tab_id": number, "scope": "agent", "url": "about:blank"}
                return {"session_id": sid}
            if args == ("session", "list"):
                return [{"session_id": sid} for sid in live]
            if args == ("tab", "list"):
                return {"tabs": [live[session]]}
            if args[0] == "navigate":
                navigations.append((session, tab_id, args[1]))
                self.assertEqual(tab_id, live[session]["tab_id"])
                live[session]["url"] = args[1]
                return dict(live[session])
            if args[:2] == ("session", "stop"):
                del live[args[2]]
                return {}
            raise AssertionError(args)

        def call(op, payload, tab_id, timeout, session_id=None):
            sid = session_id or sn.ensure_session()
            self.assertEqual(tab_id, live[sid]["tab_id"])
            calls.append((sn.LOGICAL_SESSION_ID, sid, tab_id, op))
            return {"ok": True, "data": {"status_code": 200, "stdout": payload.get("code", "")}}

        with patch.object(sn, "bsk", side_effect=bsk), patch.object(sn, "_call_js", side_effect=call), \
                patch.object(sn, "_eval_js", return_value="https://inner.dcs.cloud/notebook/st/w/lab"):
            rc, a = self.cli("session", "create", "--url", URL)
            self.assertEqual(rc, 0)
            rc, b = self.cli("session", "create", "--url", URL)
            self.assertEqual(rc, 0)
            aid, bid = a["logical_session_id"], b["logical_session_id"]
            self.assertNotEqual(aid, bid)
            self.assertNotEqual(a["browser_session_id"], b["browser_session_id"])
            self.assertNotEqual(a["tab_id"], b["tab_id"])
            self.assertNotEqual(a["state_dir"], b["state_dir"])
            for lid, marker in ((aid, "A"), (bid, "B")):
                rc, result = self.cli("--session", lid, "run-python", "--code", marker)
                self.assertEqual((rc, result["data"]["stdout"]), (0, marker))
            initial_navigation_count = len(navigations)
            for lid, original in ((aid, a), (bid, b)):
                for _ in range(3):
                    rc, result = self.cli('--session', lid, 'connect', '--url', URL)
                    self.assertEqual(rc, 0)
                    self.assertEqual(result['browser_session_id'], original['browser_session_id'])
                    self.assertEqual(result['tab_id'], original['tab_id'])
            self.assertEqual(set(live), {a['browser_session_id'], b['browser_session_id']})
            self.assertEqual(len(navigations), initial_navigation_count)
            self.assertFalse(any(row[-1] == 'cleanup' for row in calls))
            self.assertEqual(self.cli("--session", aid, "close")[0], 0)
            self.assertIn(b["browser_session_id"], live)
            self.assertEqual(self.cli("--session", bid, "probe")[0], 0)
            del live[b["browser_session_id"]]
            rc, recovered = self.cli("--session", bid, "connect")
            self.assertEqual(rc, 0)
            self.assertEqual(recovered["logical_session_id"], bid)
            self.assertNotEqual(recovered["browser_session_id"], b["browser_session_id"])
            self.assertEqual(len(self.cli("session", "list")[1]["sessions"]), 2)
            cleanup = [row for row in calls if row[-1] == "cleanup"]
            self.assertEqual([row[0] for row in cleanup], [aid])
        self.assertIsNone(sn.LOGICAL_SESSION_ID)

    def test_invalid_target_and_failed_creation_keep_browser_and_ids_safe(self):
        lid = sn.create_logical_session(URL)
        with patch.object(sn, "bsk", side_effect=AssertionError("must not contact browser")):
            self.assertEqual(self.cli("--session", "../bad", "probe")[0], 2)
            self.assertEqual(self.cli("--session", "00000000-0000-0000-0000-000000000000", "probe")[0], 2)
            self.assertEqual(self.cli("--session", lid, "connect", "--url", URL + "-other")[0], 2)
            with patch.dict(os.environ, {"SN_URL": URL + "-other"}):
                self.assertEqual(self.cli("--session", lid, "probe")[0], 2)
        with patch.object(sn, "bsk", side_effect=sn.Bridge("browser_unavailable")):
            rc, result = self.cli("session", "create", "--url", URL)
        self.assertEqual(rc, 1)
        self.assertTrue(Path(sn._logical_dir(result["logical_session_id"]), "config.json").is_file())

    def test_different_users_supply_independent_workspace_targets(self):
        alice = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=project-alice&workspaceId=workspace-alice"
        bob = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=project-bob&workspaceId=workspace-bob"
        first, second = sn.create_logical_session(alice), sn.create_logical_session(bob)
        with sn.selected_session(first):
            self.assertEqual(sn.WORKSPACE_URL, alice)
        with sn.selected_session(second):
            self.assertEqual(sn.WORKSPACE_URL, bob)
            self.assertEqual(sn.CFG["project_id"], "project-bob")
            self.assertEqual(sn.CFG["workspace_id"], "workspace-bob")

    def test_lock_blocks_another_process_and_releases_on_exit(self):
        directory = sn._logical_dir(sn.create_logical_session(URL))
        other = sn._logical_dir(sn.create_logical_session(URL))
        code = ("import importlib.util,sys; "
                "s=importlib.util.spec_from_file_location('sn_lock',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "\nwith m._controller_lock(sys.argv[2]):\n print('locked',flush=True); input()\n")
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(SCRIPT), directory],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(sn.Bridge, "session_busy"):
                with sn._controller_lock(directory, timeout=0.1):
                    self.fail("another process owns this lock")
            with sn._controller_lock(other, timeout=0):
                pass
            child.communicate("release\n", timeout=5)
            self.assertEqual(child.returncode, 0)
            with sn._controller_lock(directory, timeout=0):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
