import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sn_regressions", ROOT / "scripts/sn.py")
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)
URL_A = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=A"
URL_B = URL_A[:-1] + "B"


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(sn, "WORKSPACE_URL", URL_A))
        self.stack.enter_context(patch.object(sn, "config_path", return_value=str(Path(self.tmp.name) / "config.json")))

    def test_changed_target_does_not_reuse_previous_workspace(self):
        with patch.object(sn, "WORKSPACE_URL", URL_B), \
             patch.object(sn, "ensure_session", return_value="s"), \
             patch.object(sn, "_browser_state", return_value={"task_base": "/notebook/st/A/", "project_id": "p", "workspace_id": "A"}), \
             patch.object(sn, "_agent_tabs", return_value=[{"tab_id": 7, "url": "https://inner.dcs.cloud/notebook/st/A/lab"}]), \
             patch.object(sn, "_probe_tab", return_value=True), \
             patch.object(sn, "_save_browser_state"), \
             patch.object(sn, "bsk", side_effect=sn.Bridge("navigation_required")):
            with self.assertRaisesRegex(sn.Bridge, "navigation_required"):
                sn.ensure_tab()

    def test_workspace_browser_sessions_use_separate_state_files(self):
        first = sn._browser_state_path()
        with patch.object(sn, "WORKSPACE_URL", URL_B):
            self.assertNotEqual(first, sn._browser_state_path())

    def test_idempotency_is_scoped_to_workspace_and_independent_of_name(self):
        first = sn.make_job_id("first", "same-key")
        self.assertEqual(first, sn.make_job_id("renamed", "same-key"))
        with patch.object(sn, "WORKSPACE_URL", URL_B):
            self.assertNotEqual(first, sn.make_job_id("first", "same-key"))

    def test_reused_key_rejects_changed_command_or_cwd(self):
        cmd = "echo original"
        jid = sn.make_job_id(None, "key")
        sn._write_receipt(jid, "submitted", project_id="p", workspace_id="A", cwd=sn.WORK_ROOT,
                          command_sha256=hashlib.sha256(cmd.encode()).hexdigest())
        with patch.object(sn, "run_shell") as dispatch:
            for command, cwd in [("echo other", None), (cmd, "/data/work/other")]:
                with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
                    sn.submit(command, cwd=cwd, idempotency_key="key")
            dispatch.assert_not_called()

    def test_large_text_uses_real_failure_envelope_before_fallback(self):
        with patch.object(sn, "ensure_tab", return_value=1), \
             patch.object(sn, "_call_js", return_value={"ok": False, "error": "too_large", "data": {"ok": False, "error": "too_large", "size": 20000}}), \
             patch.object(sn, "run_python", return_value={"data": {"stdout": 'INSPECT_JSON={"head":"prefix","truncated":true}'}}) as fallback:
            self.assertEqual(sn.cat_path("work/stdout.log")["head"], "prefix")
            fallback.assert_called_once()

    def test_preview_does_not_hide_authentication_errors(self):
        with patch.object(sn, "ensure_tab", return_value=1), \
             patch.object(sn, "_call_js", return_value={"ok": False, "error": "read_failed", "data": {"status": 403}}), \
             patch.object(sn, "run_python") as fallback:
            with self.assertRaises(sn.Bridge):
                sn.cat_path("work/private.txt")
            fallback.assert_not_called()

    def download(self, size, chunk=None):
        self.stack.enter_context(patch.object(sn, "ensure_tab", return_value=1))
        self.stack.enter_context(patch.object(sn, "ensure_session", return_value="s"))
        self.stack.enter_context(patch.object(sn, "_remote_stat", return_value={"size": size, "last_modified": "t"}))
        self.stack.enter_context(patch.object(sn, "_remote_sha256", return_value=None))
        self.stack.enter_context(patch.object(sn, "_call_js", side_effect=chunk))

    def test_empty_download_creates_an_empty_final_file(self):
        self.download(0)
        out = Path(self.tmp.name) / "empty.bin"
        self.assertTrue(sn.get_file("work/empty.bin", str(out))["ok"])
        self.assertEqual(out.read_bytes(), b"")

    def test_download_preserves_file_created_after_initial_check(self):
        out = Path(self.tmp.name) / "race.bin"
        def chunk(*args):
            out.write_bytes(b"keep")
            return {"ok": True, "data": {"b64": base64.b64encode(b"x").decode()}}
        self.download(1, chunk)
        with self.assertRaisesRegex(ValueError, "local_file_exists"):
            sn.get_file("work/race.bin", str(out))
        self.assertEqual(out.read_bytes(), b"keep")

    def test_resume_never_reuses_a_partial_from_another_workspace(self):
        out = Path(self.tmp.name) / "resume.bin"
        Path(str(out) + ".part").write_bytes(b"A")
        Path(str(out) + ".part.json").write_text(json.dumps({
            "remote": "/data/work/resume.bin", "size": 1, "last_modified": "t"}))
        self.download(1, lambda *args: {"ok": True, "data": {"b64": base64.b64encode(b"B").decode()}})
        with patch.object(sn, "WORKSPACE_URL", URL_B):
            result = sn.get_file("work/resume.bin", str(out), verify=False)
        self.assertEqual(result["resumed_from"], 0)
        self.assertEqual(out.read_bytes(), b"B")

    def test_binary_put_uses_exclusive_create_and_readback(self):
        local = Path(self.tmp.name) / "input.bin"
        local.write_bytes(b"\x00\xffbinary")
        response = {"data": {"stdout": "__SN_WRITE_fixed__:" + base64.b64encode(json.dumps({"ok": True}).encode()).decode()}}
        with patch.object(sn.uuid, "uuid4") as uid, \
             patch.object(sn, "evaluate_op", return_value=response) as evaluate, \
             patch.object(sn, "ensure_tab", return_value=1), \
             patch.object(sn, "ensure_session", return_value="s"), \
             patch.object(sn, "_remote_stat", return_value={"size": local.stat().st_size}), \
             patch.object(sn, "_call_js", return_value={"ok": True, "data": {}}):
            uid.return_value.hex = "fixed"
            sn.put_file(str(local), "work/result.bin")
        self.assertTrue(evaluate.called, "put must use the atomic kernel writer")
        code = evaluate.call_args.args[1]["code"]
        self.assertIn("os.O_EXCL", code)
        self.assertIn("write_readback_mismatch", code)

    def test_atomic_binary_writer_roundtrips_and_preserves_a_competing_file(self):
        root = Path(self.tmp.name).as_posix()
        target = Path(self.tmp.name) / "binary.bin"
        raw = bytes(range(256)) * 2
        def execute(op, payload, timeout):
            capture = io.StringIO()
            native_open = os.open
            def binary_open(path, flags, mode=0o777):
                return native_open(path, flags | getattr(os, "O_BINARY", 0), mode)
            with contextlib.redirect_stdout(capture), patch.object(os, "fchmod", create=True), \
                 patch.object(os, "open", side_effect=binary_open):
                exec(payload["code"], {})
            return {"ok": True, "data": {"stdout": capture.getvalue()}}
        with patch.object(sn, "to_server", return_value=target.as_posix()), \
             patch.object(sn, "evaluate_op", side_effect=execute):
            sn._write_bytes(target.as_posix(), raw, False, root)
            self.assertEqual(target.read_bytes(), raw)
            with self.assertRaisesRegex(sn.Bridge, "file_exists_use_overwrite"):
                sn._write_bytes(target.as_posix(), b"replace", False, root)
            self.assertEqual(target.read_bytes(), raw)

    def test_concurrent_same_key_dispatches_once(self):
        dispatched = threading.Event()
        release = threading.Event()
        jid = sn.make_job_id(None, "concurrent")
        def run(*args, **kwargs):
            dispatched.set()
            if not release.wait(5):
                raise AssertionError("test did not release dispatch")
            return {"ok": True, "returncode": 0, "stdout": "__SN_SUBMITTED__=" + jid}
        with patch.object(sn, "run_shell", side_effect=run) as dispatch, \
             patch.object(sn, "_wait_for_runner_ready", return_value=None), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(sn.submit, "echo once", idempotency_key="concurrent")
            try:
                self.assertTrue(dispatched.wait(3))
                second = sn.submit("echo once", name="renamed", idempotency_key="concurrent")
                self.assertTrue(second["reused"])
            finally:
                release.set()
            self.assertFalse(first.result(timeout=5)["reused"])
            self.assertEqual(dispatch.call_count, 1)

    def test_ci_does_not_reference_a_missing_requirements_file(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text()
        self.assertTrue("pip install -r requirements.txt" not in workflow or (ROOT / "requirements.txt").is_file())
        self.assertIn("node --test tests/runtime.test.cjs", workflow)


if __name__ == "__main__":
    unittest.main()
