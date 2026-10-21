import importlib.util
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import subprocess
import shutil
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sn.py"
SPEC = importlib.util.spec_from_file_location("sn", SCRIPT)
sn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sn)


def setUpModule():
    """Keep tests away from the real user config: browser state and job receipts."""
    global _TMP, _PATCHES
    _TMP = tempfile.TemporaryDirectory()
    _PATCHES = [
        patch.object(sn, "WORKSPACE_URL", "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"),
        patch.object(sn, "config_path", return_value=os.path.join(_TMP.name, "config.json")),
        patch.object(sn, "_browser_state_path",
                     return_value=os.path.join(_TMP.name, "browser.json")),
    ]
    for patcher in _PATCHES:
        patcher.start()


def tearDownModule():
    for patcher in _PATCHES:
        patcher.stop()
    _TMP.cleanup()


class JobValidationTests(unittest.TestCase):
    def test_validate_job_id_accepts_canonical_uuid_id(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"

        self.assertEqual(sn.validate_job_id(job_id), job_id)

    def test_validate_job_id_rejects_shell_metacharacters(self):
        for value in ("job;id", "job id", "job$(whoami)", "job/../x", "job`id`", "job|cat"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sn.validate_job_id(value)

    def test_make_job_id_returns_a_uuid_based_id(self):
        job_id = sn.make_job_id()

        self.assertRegex(job_id, r"^job_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(sn.validate_job_id(job_id), job_id)

    def test_validate_tail_bounds_values(self):
        self.assertEqual(sn.validate_tail(1), 1)
        self.assertEqual(sn.validate_tail("40"), 40)
        self.assertEqual(sn.validate_tail(200), 200)
        for value in (0, -1, 201, "many", "40; rm -rf /"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sn.validate_tail(value)


class JobLauncherTests(unittest.TestCase):
    def test_launcher_creates_durable_job_record_files(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "python analysis.py")

        self.assertIn("setsid", launcher)
        self.assertIn("status.json", launcher)
        self.assertIn('"job_550e8400-e29b-41d4-a716-446655440000"', launcher)
        self.assertIn("stdout.log", launcher)
        self.assertIn("stderr.log", launcher)
        self.assertIn("exit_code", launcher)
        self.assertIn("python analysis.py", launcher)

    def test_launcher_uses_a_unique_heredoc_delimiter_for_command_content(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        command = "echo first\n__SN_COMMAND__\necho still-in-command"
        launcher = sn.build_job_launcher(job_id, command)

        match = re.search(r"cmd\.sh <<'([^']+)'\n(.*?)\n\1\n", launcher, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertNotEqual(match.group(1), "__SN_COMMAND__")
        self.assertEqual(match.group(2), command)

    def test_launcher_creates_job_directory_exclusively(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "echo safe")

        self.assertIn(f"mkdir {sn.JOB_ROOT}/{job_id}", launcher)
        self.assertNotIn("mkdir -p", launcher)

    def test_launcher_stops_on_setup_failures_and_prepares_artifacts(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "pwd > working-directory.txt")
        setup = launcher.split("cat >", 1)[0]

        self.assertIn("set -e", launcher)
        self.assertIn(f"mkdir {sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertNotIn("status.json.tmp && mv", setup)
        self.assertLess(launcher.index("mkdir " + sn.JOB_ROOT + "/" + job_id + "/artifacts"),
                        launcher.index("setsid"))

    def test_launcher_explicitly_restricts_controller_and_artifact_permissions(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "echo safe")
        self.assertIn("umask 077", launcher)
        self.assertIn(f"chmod 700 {sn.JOB_ROOT}/{job_id}", launcher)
        self.assertIn(f"chmod 700 {sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertIn(f"chmod 600 {sn.JOB_ROOT}/{job_id}/manifest.json", launcher)
        self.assertIn("os.chmod(path, 0o600)", launcher)

    def test_launcher_runs_payload_from_explicit_server_cwd_and_exports_job_dirs(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launcher = sn.build_job_launcher(job_id, "pwd", cwd="/data/work/project")

        self.assertIn("cd /data/work/project || exit 72", launcher)
        self.assertIn(f"export SN_JOB_DIR={sn.JOB_ROOT}/{job_id}", launcher)
        self.assertIn(f"export SN_ARTIFACT_DIR={sn.JOB_ROOT}/{job_id}/artifacts", launcher)
        self.assertIn('"cwd":"/data/work/project"', launcher)


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._receipts = tempfile.TemporaryDirectory()
        self.addCleanup(self._receipts.cleanup)
        patcher = patch.object(sn, "_receipts_dir", return_value=self._receipts.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_submit_uses_a_generated_validated_id_and_durable_launcher(self):
        job_id = "analysis_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "make_job_id", return_value=job_id) as make_id, \
             patch.object(sn, "build_job_launcher", return_value="safe launcher") as build, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": f"__SN_SUBMITTED__={job_id}",
                                                           "stderr": ""}) as run, \
             patch.object(sn, "poll", return_value={"status": {"state": "running"}}) as poll:
            result = sn.submit("python analysis.py", name="analysis")

        make_id.assert_called_once_with("analysis", None)
        build.assert_called_once_with(job_id, "python analysis.py", cwd=None)
        run.assert_called_once_with("safe launcher", cwd=sn.WORK_ROOT, timeout=sn.MAX_SYNC_SECONDS)
        poll.assert_called_once_with(job_id, tail=1)
        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["dir"], f"{sn.JOB_ROOT}/{job_id}")

    def test_submit_raises_when_launcher_setup_fails(self):
        with patch.object(sn, "make_job_id", return_value="job_550e8400-e29b-41d4-a716-446655440000"), \
             patch.object(sn, "build_job_launcher", return_value="launcher"), \
             patch.object(sn, "run_shell", return_value={"ok": False, "returncode": 1,
                                                           "stderr": "mkdir: permission denied"}):
            with self.assertRaisesRegex(sn.Bridge, "job_setup_failed"):
                sn.submit("echo work")

    def test_rds_inspection_polls_the_id_returned_by_submit(self):
        job_id = "rdsinspect_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "submit", return_value={"job_id": job_id}) as submit, \
             patch.object(sn, "poll", return_value={
                 "status": {"state": "finished", "exit_code": 0},
                 "stdout_tail": "INSPECT_JSON={\"type\": \"generic\"}",
                 "stderr_tail": "", "read_errors": {},
             }) as poll:
            result = sn.inspect_rds("/data/work/input.rds", timeout=1)

        self.assertEqual(result, {"type": "generic"})
        self.assertEqual(poll.call_args.args[0], job_id)
        self.assertIn("Rscript -e", submit.call_args.args[0])

    def test_poll_rejects_invalid_job_id_and_tail_before_remote_shell(self):
        with patch.object(sn, "run_shell") as run:
            with self.assertRaises(ValueError):
                sn.poll("job; rm -rf /", tail=40)
            with self.assertRaises(ValueError):
                sn.poll("job_550e8400-e29b-41d4-a716-446655440000", tail="1;id")
        run.assert_not_called()

    def test_poll_reads_status_and_bounded_log_tails(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        status = base64.b64encode(b'{"state":"finished","exit_code":0}').decode()
        stdout = base64.b64encode(b'last output').decode()
        stderr = base64.b64encode(b'last error').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {status}",
                          f"__SN_POLL_fixed__ stdout ok {stdout}",
                          f"__SN_POLL_fixed__ stderr ok {stderr}",
                          "__SN_POLL_fixed__ dir ok ",
                          "__SN_POLL_fixed__ live ok b3duZWQ6MTIzNA=="))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}) as run:
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id, tail=7)

        command = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs.get("max_bytes"), sn.MAX_INTERNAL_SHELL_BYTES)
        self.assertIn("status.json", command)
        self.assertIn("tail -n 7", command)
        self.assertIn("tail -c", command)
        self.assertIn("stdout.log", command)
        self.assertIn("stderr.log", command)
        self.assertEqual(result, {"job_id": job_id, "tail": 7,
                                  "status": {"state": "finished", "exit_code": 0},
                                  "stdout_tail": "last output", "stderr_tail": "last error",
                                  "missing": {"status": False, "stdout": False,
                                              "stderr": False, "dir": False, "live": False},
                                  "read_errors": {}, "job_dir_present": True,
                                  "local_state": None, "outcome": "finished",
                                  "process": "owned", "pid": 1234})

    def test_poll_reports_missing_and_invalid_status_without_mixing_log_headings(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        invalid = base64.b64encode(b'not json').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {invalid}",
                          "__SN_POLL_fixed__ stdout missing ",
                          "__SN_POLL_fixed__ stderr missing ",
                          "__SN_POLL_fixed__ dir ok ",
                          "__SN_POLL_fixed__ live ok b3duZWQ6MTIzNA=="))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id)

        self.assertIsNone(result["status"])
        self.assertEqual(result["stdout_tail"], "")
        self.assertEqual(result["stderr_tail"], "")
        self.assertEqual(result["missing"],
                         {"status": False, "stdout": True, "stderr": True,
                          "dir": False, "live": False})
        self.assertIn("status", result["read_errors"])


    def test_submit_persists_the_job_id_before_dispatch(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        seen = []

        def fake_run_shell(*args, **kwargs):
            seen.append(sn._read_receipt(job_id))
            raise sn.Bridge("bsk_timeout(evaluate)")

        with patch.object(sn, "make_job_id", return_value=job_id), \
             patch.object(sn, "build_job_launcher", return_value="launcher"), \
             patch.object(sn, "run_shell", side_effect=fake_run_shell):
            with self.assertRaisesRegex(sn.Bridge, f"submit_unknown: job_id={job_id}"):
                sn.submit("echo work")
        self.assertEqual(seen[0]["state"], "pending")
        self.assertEqual(sn._read_receipt(job_id)["state"], "unknown")

    def test_idempotency_key_reuses_the_previous_job_without_dispatching(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        sn._write_receipt(job_id, "submitted", cwd=sn.WORK_ROOT, project_id="p", workspace_id="w",
                          command_sha256=hashlib.sha256(b"echo work").hexdigest())
        with patch.object(sn, "make_job_id", return_value=job_id) as make_id, \
             patch.object(sn, "run_shell") as run:
            result = sn.submit("echo work", idempotency_key="run-1")
        make_id.assert_called_once_with(None, "run-1")
        run.assert_not_called()
        self.assertTrue(result["reused"])
        self.assertEqual(result["state"], "submitted")

    def test_idempotency_key_derives_a_stable_job_id(self):
        first = sn.make_job_id(None, "same-key")
        self.assertEqual(first, sn.make_job_id(None, "same-key"))
        self.assertNotEqual(first, sn.make_job_id(None, "other-key"))
        sn.validate_job_id(first)

    def test_jobs_merges_local_receipts_with_the_server_registry(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        sn._write_receipt(job_id, "submitted")
        sn.accept_job(job_id, note="looks right")
        local_only = sn.list_jobs()
        self.assertEqual(local_only["count"], 1)
        self.assertEqual(local_only["jobs"][0]["state"], "local_only")
        self.assertEqual(local_only["jobs"][0]["acceptance_note"], "looks right")
        remote = {"jobs": [{"job_id": job_id, "state": "finished", "exit_code": 0,
                            "cwd": "/data/work", "artifacts": {"count": 2, "bytes": 9}}]}
        with patch.object(sn, "run_python",
                          return_value={"data": {"stdout": "JOBS_JSON=" + json.dumps(remote)}}):
            merged = sn.list_jobs(remote=True)
        self.assertEqual(merged["count"], 1)
        entry = merged["jobs"][0]
        self.assertEqual(entry["state"], "finished")
        self.assertEqual(entry["local_state"], "submitted")
        self.assertIn("accepted_at", entry)
        self.assertEqual(entry["artifacts"], {"count": 2, "bytes": 9})

    def test_poll_reports_not_dispatched_when_only_a_pending_receipt_exists(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        sn._write_receipt(job_id, "pending")
        raw = "\n".join(("__SN_POLL_fixed__ status missing ",
                         "__SN_POLL_fixed__ stdout missing ",
                         "__SN_POLL_fixed__ stderr missing ",
                         "__SN_POLL_fixed__ dir missing ",
                         "__SN_POLL_fixed__ live ok "))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                          "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id)
        self.assertEqual(result["outcome"], "not_dispatched")
        self.assertEqual(result["local_state"], "pending")


class BinaryTransferTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = os.path.join(self._tmp.name, "out.bin")

    def _stream(self, data):
        starts = []

        def fake(op, payload, tab_id, timeout, session_id=None):
            self.assertEqual(op, "fetch_range")
            block = data[payload["start"]:payload["end"] + 1]
            starts.append(payload["start"])
            return {"ok": True, "data": {"b64": base64.b64encode(block).decode("ascii")}}

        return starts, fake

    def test_get_streams_chunks_verifies_sha256_and_keeps_no_part(self):
        data = bytes(range(256)) * (sn.TRANSFER_CHUNK // 128)
        starts, fake = self._stream(data)
        with patch.object(sn, "ensure_tab", return_value=1),              patch.object(sn, "ensure_session", return_value="s1"),              patch.object(sn, "_remote_stat",
                          return_value={"size": len(data), "last_modified": "t"}),              patch.object(sn, "_remote_sha256",
                          return_value=hashlib.sha256(data).hexdigest()),              patch.object(sn, "_call_js", side_effect=fake):
            result = sn.get_file("work/x.bin", self.out)
        self.assertEqual(starts, [0, sn.TRANSFER_CHUNK])
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(result["verified"], "sha256")
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_get_resumes_from_a_partial_with_a_matching_record(self):
        data = b"abc" * (sn.TRANSFER_CHUNK // 3 * 2)
        half = sn.TRANSFER_CHUNK
        with open(self.out + ".part", "wb") as fh:
            fh.write(data[:half])
        with open(self.out + ".part.json", "w", encoding="utf-8") as fh:
            json.dump({"project_id": "p", "workspace_id": "w",
                       "remote": "/data/work/x.bin", "size": len(data),
                       "last_modified": "t"}, fh)
        starts, fake = self._stream(data)
        with patch.object(sn, "ensure_tab", return_value=1),              patch.object(sn, "ensure_session", return_value="s1"),              patch.object(sn, "_remote_stat",
                          return_value={"size": len(data), "last_modified": "t"}),              patch.object(sn, "_remote_sha256", return_value=None),              patch.object(sn, "_call_js", side_effect=fake):
            result = sn.get_file("work/x.bin", self.out)
        self.assertEqual(result["resumed_from"], half)
        self.assertEqual(starts, [half])
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), data)

    def test_get_detects_a_source_changed_mid_transfer(self):
        data = b"x" * 1024
        _, fake = self._stream(data)
        stats = [{"size": len(data), "last_modified": "before"},
                 {"size": len(data), "last_modified": "after"}]
        with patch.object(sn, "ensure_tab", return_value=1),              patch.object(sn, "ensure_session", return_value="s1"),              patch.object(sn, "_remote_stat", side_effect=stats),              patch.object(sn, "_call_js", side_effect=fake):
            with self.assertRaisesRegex(sn.Bridge, "source_changed_during_transfer"):
                sn.get_file("work/x.bin", self.out)
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_get_rejects_a_checksum_mismatch(self):
        data = b"y" * 1024
        _, fake = self._stream(data)
        with patch.object(sn, "ensure_tab", return_value=1),              patch.object(sn, "ensure_session", return_value="s1"),              patch.object(sn, "_remote_stat",
                          return_value={"size": len(data), "last_modified": "t"}),              patch.object(sn, "_remote_sha256", return_value="0" * 64),              patch.object(sn, "_call_js", side_effect=fake):
            with self.assertRaisesRegex(sn.Bridge, "transfer_checksum_mismatch"):
                sn.get_file("work/x.bin", self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_get_refuses_to_overwrite_a_local_file(self):
        with open(self.out, "wb") as fh:
            fh.write(b"keep")
        with patch.object(sn, "ensure_tab", return_value=1),              patch.object(sn, "ensure_session", return_value="s1"),              patch.object(sn, "_remote_stat",
                          return_value={"size": 4, "last_modified": "t"}):
            with self.assertRaisesRegex(ValueError, "local_file_exists_use_overwrite"):
                sn.get_file("work/x.bin", self.out)
        with open(self.out, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")

    def test_get_rejects_paths_outside_the_contents_root(self):
        with self.assertRaisesRegex(ValueError, "contents_root"):
            sn.get_file("/mnt/reference/x.bin", self.out)

    def test_put_uploads_and_verifies_the_remote_size(self):
        local = os.path.join(self._tmp.name, "in.bin")
        data = b"payload" * 100
        with open(local, "wb") as fh:
            fh.write(data)
        with patch.object(sn, "_write_bytes", return_value={"ok": True}) as call, patch.object(sn, "_remote_stat",
                          return_value={"size": len(data), "last_modified": "t"}):
            result = sn.put_file(local, "work/x.bin")
        self.assertEqual(result["bytes"], len(data))
        uploaded = call.call_args.args[1]
        self.assertEqual(uploaded, data)

    def test_put_surfaces_no_clobber_and_size_limits(self):
        local = os.path.join(self._tmp.name, "in.bin")
        with open(local, "wb") as fh:
            fh.write(b"12345")
        with patch.object(sn, "_write_bytes", side_effect=sn.Bridge("file_exists_use_overwrite")):
            with self.assertRaisesRegex(sn.Bridge, "file_exists_use_overwrite"):
                sn.put_file(local, "work/x.bin")
        with patch.object(sn, "MAX_UPLOAD_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "upload_too_large"):
                sn.put_file(local, "work/x.bin")


class JobRegistryTests(unittest.TestCase):
    def setUp(self):
        self._receipts = tempfile.TemporaryDirectory()
        self.addCleanup(self._receipts.cleanup)
        patcher = patch.object(sn, "_receipts_dir", return_value=self._receipts.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    job_id = "job_550e8400-e29b-41d4-a716-446655440000"

    def test_cancel_refuses_a_pid_that_does_not_own_the_job_directory(self):
        with patch.object(sn, "run_shell",
                          return_value={"ok": False, "returncode": 15,
                                        "stdout": "pid_not_owned\n", "stderr": ""}):
            result = sn.cancel_job(self.job_id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "pid_not_owned")

    def test_cancel_signals_the_owned_process_group_only(self):
        with patch.object(sn, "run_shell",
                          return_value={"ok": True, "returncode": 0,
                                        "stdout": "terminated:4242\n", "stderr": ""}) as run:
            result = sn.cancel_job(self.job_id)
        self.assertEqual(result["signal"], "SIGTERM")
        self.assertEqual(result["pid"], 4242)
        command = run.call_args.args[0]
        self.assertIn("/proc/$pid/cmdline", command)
        self.assertIn('*"$jdir"*', command)

    def test_accept_records_the_human_decision(self):
        result = sn.accept_job(self.job_id, note="verified")
        receipt = sn._read_receipt(self.job_id)
        self.assertEqual(receipt["acceptance_note"], "verified")
        self.assertEqual(receipt["accepted_at"], result["accepted_at"])

    def _poll_raw(self, state, live):
        status = base64.b64encode(json.dumps({"state": state}).encode()).decode()
        live_payload = base64.b64encode(live.encode()).decode()
        return "\n".join((f"__SN_POLL_fixed__ status ok {status}",
                          "__SN_POLL_fixed__ stdout ok " + base64.b64encode(b"").decode(),
                          "__SN_POLL_fixed__ stderr ok " + base64.b64encode(b"").decode(),
                          "__SN_POLL_fixed__ dir ok ",
                          f"__SN_POLL_fixed__ live ok {live_payload}"))

    def _poll(self, raw):
        with patch.object(sn.uuid, "uuid4") as make_uuid,              patch.object(sn, "run_shell",
                          return_value={"ok": True, "returncode": 0,
                                        "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            return sn.poll(self.job_id)

    def test_poll_marks_a_running_status_with_a_dead_pid_as_stale(self):
        result = self._poll(self._poll_raw("running", "dead:999"))
        self.assertEqual(result["outcome"], "stale_running")
        self.assertEqual(result["process"], "dead")
        self.assertEqual(result["pid"], 999)

    def test_poll_does_not_claim_running_without_a_live_check(self):
        result = self._poll(self._poll_raw("running", ""))
        self.assertEqual(result["outcome"], "running_unverified")


class LayeredDiagnosticsTests(unittest.TestCase):
    url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
    task_base = "/notebook/st/task-p/"
    inner = "https://inner.dcs.cloud/notebook/st/task-p/lab"

    def _bsk(self, sessions=("s1",), browsers=(("edge",),), fail=None):
        def fake(*args, timeout=120, session=None, tab_id=None):
            if fail:
                raise sn.Bridge(fail)
            if args[:2] == ("session", "list"):
                return [{"session_id": item} for item in sessions]
            if args[:1] == ("browsers",):
                return [{"browser_name": item[0], "browser_version": "155",
                         "extension_version": "0.3.3", "unresponsive": False}
                        for item in browsers]
            raise AssertionError(args)
        return fake

    def _deep(self, state=None, probe=True, probe_error=None, **kwargs):
        if state is None:
            state = {"session_id": "s1", "tab_id": 7, "task_base": self.task_base}

        def fake_probe(*args, **call_kwargs):
            if probe_error:
                raise sn.Bridge(probe_error)
            return probe

        with patch.object(sn, "_bsk_version", return_value="bsk 0.3.3"), \
             patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value=state), \
             patch.object(sn, "bsk", side_effect=self._bsk(**kwargs)), \
             patch.object(sn, "_agent_tabs",
                          return_value=[{"tab_id": 7, "url": self.inner, "scope": "agent"}]), \
             patch.object(sn, "_probe_tab", side_effect=fake_probe):
            return sn.doctor(deep=True)

    @staticmethod
    def _statuses(info):
        return {layer["layer"]: layer["status"] for layer in info["layers"]}

    def test_healthy_chain_reports_every_layer_ok(self):
        info = self._deep()
        self.assertTrue(info["ok"])
        self.assertEqual(self._statuses(info),
                         {"cli": "ok", "daemon": "ok", "extension": "ok", "workspace": "ok",
                          "session": "ok", "tab": "ok", "jupyter": "ok"})
        self.assertIsNone(info["next_action"])

    def test_daemon_down_stops_at_the_daemon_layer(self):
        info = self._deep(fail="bsk_unavailable: no daemon")
        statuses = self._statuses(info)
        self.assertFalse(info["ok"])
        self.assertEqual(statuses["daemon"], "failed")
        self.assertEqual(statuses["extension"], "skipped")
        self.assertEqual(statuses["jupyter"], "skipped")
        self.assertIn("bsk daemon", info["next_action"])

    def test_disconnected_extension_is_distinct_from_a_dead_daemon(self):
        info = self._deep(browsers=())
        statuses = self._statuses(info)
        self.assertEqual(statuses["daemon"], "ok")
        self.assertEqual(statuses["extension"], "failed")
        self.assertIn("扩展", info["next_action"])

    def test_expired_login_is_reported_at_the_jupyter_layer(self):
        info = self._deep(probe_error="jupyter_auth_required: HTTP 403")
        statuses = self._statuses(info)
        self.assertEqual(statuses["tab"], "ok")
        self.assertEqual(statuses["jupyter"], "failed")
        self.assertIn("登录", info["next_action"])

    def test_reaped_session_is_stale_but_not_a_failure(self):
        info = self._deep(sessions=(), state={"session_id": "old", "tab_id": None,
                                              "task_base": self.task_base})
        statuses = self._statuses(info)
        self.assertTrue(info["ok"])
        self.assertEqual(statuses["session"], "stale")
        self.assertEqual(statuses["jupyter"], "skipped")
        self.assertIn("自动重开", info["next_action"])


class ArtifactInventoryTests(unittest.TestCase):
    def test_artifacts_rejects_invalid_ids_before_remote_execution(self):
        with patch.object(sn, "run_python") as run:
            with self.assertRaises(ValueError):
                sn.artifacts("job/../escape")
        run.assert_not_called()

    def test_artifacts_returns_only_regular_files_with_size_and_sha256(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        payload = {"files": [{"path": "result.txt", "size": 5,
                              "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}]}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "ARTIFACTS_JSON=" + json.dumps(payload)}}) as run:
            result = sn.artifacts(job_id)

        code = run.call_args.args[0]
        self.assertIn("artifacts", code)
        self.assertIn("S_ISREG", code)
        self.assertEqual(result, {"job_id": job_id, "files": payload["files"]})

    def test_oversized_inventory_propagates_explicit_limit_error(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        payload = {"files": [{"path": "x" * sn.MAX_INSPECT_RESULT_BYTES,
                               "size": 1, "sha256": "0" * 64}]}
        response = {"data": {"stdout": "ARTIFACTS_JSON=" + json.dumps(payload)}}
        with patch.object(sn, "run_python", return_value=response):
            result = sn.artifacts(job_id)

        self.assertEqual(result, {
            "job_id": job_id,
            "error": "bounded_summary_too_large",
            "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES,
        })
        self.assertNotIn("files", result)
        self.assertIn("MAX_RESULT_BYTES", sn._ARTIFACT_INVENTORY)

    def test_inventory_rejects_a_symlinked_artifacts_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            root = os.path.join(tmp, "artifacts")
            os.mkdir(target)
            with open(os.path.join(target, "outside.txt"), "w", encoding="utf-8") as fh:
                fh.write("outside")
            try:
                os.symlink(target, root, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(sn._ARTIFACT_INVENTORY % {"root": root}, {})

        payload = json.loads(output.getvalue().split("=", 1)[1])
        self.assertEqual(payload["files"], [])

    def test_cli_artifacts_dispatches_to_inventory(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "artifacts", return_value={"job_id": job_id, "files": []}) as inventory, \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["artifacts", job_id]), 0)
        inventory.assert_called_once_with(job_id)
        output.assert_called_once_with({"job_id": job_id, "files": []})


class BoundedInspectionTests(unittest.TestCase):
    def test_text_preview_uses_server_side_byte_limit(self):
        with patch.object(sn, "run_python", return_value={"data": {"stdout": (
                'INSPECT_JSON={"format":"text","path":"/mnt/reference/a.py",'
                '"bytes_read":8,"truncated":true,"head":"print(1)"}'
        )}}) as run:
            result = sn.inspect_text("/mnt/reference/a.py", max_bytes=8)

        self.assertEqual(result["bytes_read"], 8)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["head"], "print(1)")
        code = run.call_args.args[0]
        self.assertIn("read(limit + 1)", code)
        self.assertIn("/mnt/reference/a.py", code)
        self.assertNotIn("evaluate_op", str(run.mock_calls))

    def test_text_preview_rejects_limits_outside_the_bound(self):
        with patch.object(sn, "run_python") as run:
            with self.assertRaises(ValueError):
                sn.inspect_text("/data/work/a.py", max_bytes=0)
            with self.assertRaises(ValueError):
                sn.inspect_text("/data/work/a.py", max_bytes=sn.MAX_INSPECT_BYTES + 1)
        run.assert_not_called()

    def test_notebook_summary_is_server_side_and_omits_outputs(self):
        payload = {"format": "ipynb", "path": "/data/work/a.ipynb", "nbformat": 4,
                   "kernel": "python3", "n_cells": 1,
                   "cells": [{"i": 0, "type": "code", "lines": 1, "preview": "x = 1"}]}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "INSPECT_JSON=" + json.dumps(payload)}}) as run:
            result = sn.inspect_ipynb("/data/work/a.ipynb")

        self.assertEqual(result, payload)
        code = run.call_args.args[0]
        self.assertIn("read(limit + 1)", code)
        self.assertIn("pop('outputs', None)", code)
        self.assertNotIn("api/contents", code)

    def test_inspect_routes_notebooks_and_text_to_bounded_helpers(self):
        with patch.object(sn, "inspect_ipynb", return_value={"format": "ipynb"}) as notebook, \
             patch.object(sn, "inspect_text", return_value={"format": "text"}) as text:
            self.assertEqual(sn.inspect("work/a.ipynb"), {"format": "ipynb"})
            self.assertEqual(sn.inspect("work/a.py"), {"format": "text"})
        notebook.assert_called_once_with("work/a.ipynb")
        text.assert_called_once_with("work/a.py")


class KernelLifecycleTests(unittest.TestCase):
    def test_work_paths_use_the_contents_api_without_a_kernel(self):
        value = {"ok": True, "data": {"ok": True, "entries": [
            {"name": "a.py", "type": "file", "size": 8}], "truncated": False}}
        with patch.object(sn, "evaluate_op", return_value=value) as evaluate,              patch.object(sn, "run_python") as run:
            listing = sn.list_path("work")
        evaluate.assert_called_once_with("list", {"path": "work"}, timeout=sn.MAX_SYNC_SECONDS)
        run.assert_not_called()
        self.assertEqual(listing["path"], "/data/work")
        self.assertEqual(listing["entries"][0]["name"], "a.py")

    def test_ls_all_forces_the_kernel_scan_for_dotfiles(self):
        payload = {"format": "directory", "path": "/data/work", "entries": []}
        source = 'LIST_JSON=' + json.dumps(payload)
        with patch.object(sn, "evaluate_op") as evaluate,              patch.object(sn, "run_python", return_value={"data": {"stdout": source}}) as run:
            self.assertEqual(sn.list_path("work", include_hidden=True), payload)
        evaluate.assert_not_called()
        run.assert_called_once()

    def test_text_preview_uses_the_contents_api_when_the_file_fits(self):
        value = {"ok": True, "data": {"ok": True, "content": "print(1)\n", "size": 9}}
        with patch.object(sn, "evaluate_op", return_value=value) as evaluate,              patch.object(sn, "run_python") as run:
            preview = sn.inspect_text("work/a.py", max_bytes=64)
        evaluate.assert_called_once_with("read", {"path": "work/a.py", "max_bytes": 64},
                                         timeout=sn.MAX_SYNC_SECONDS)
        run.assert_not_called()
        self.assertEqual(preview["head"], "print(1)\n")
        self.assertFalse(preview["truncated"])

    def test_oversized_files_fall_back_to_the_bounded_kernel_reader(self):
        value = {"ok": True, "data": {"ok": False, "error": "too_large", "size": 99}}
        preview = {"data": {"stdout": (
            'INSPECT_JSON={"format":"text","path":"/data/work/a.py",'
            '"bytes_read":8,"truncated":true,"head":"print(1)"}')}}
        with patch.object(sn, "evaluate_op", return_value=value),              patch.object(sn, "run_python", return_value=preview) as run:
            result = sn.inspect_text("work/a.py", max_bytes=8)
        run.assert_called_once()
        self.assertTrue(result["truncated"])

    def test_close_deletes_controller_kernels_before_stopping_the_session(self):
        with patch.object(sn, "_browser_state", return_value={
                "session_id": "s1", "tab_id": 7, "task_base": "/notebook/st/x/"}),              patch.object(sn, "_session_ids", return_value={"s1"}),              patch.object(sn, "_call_js",
                          return_value={"ok": True, "data": {"deleted": []}}) as call,              patch.object(sn, "bsk") as bsk_call,              patch.object(sn, "_save_browser_state"):
            result = sn.close_tabs()
        self.assertEqual(call.call_args.args[0], "cleanup")
        self.assertEqual(bsk_call.call_args.args[:3], ("session", "stop", "s1"))
        self.assertEqual(result["closed"], 1)

    def test_runtime_reuses_one_owned_kernel_and_never_touches_user_kernels(self):
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("__snKernelId", source)
        self.assertIn("reapOwned", source)
        self.assertIn('kernel.created ? "created" : "reused"', source)
        self.assertNotIn('out.kernel = "deleted:"', source)


class InspectPathSafetyTests(unittest.TestCase):
    def test_server_paths_can_read_any_linux_path_the_dcs_account_can_access(self):
        self.assertEqual(sn.to_server("work/a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("/data/work/a.h5ad"), "/data/work/a.h5ad")
        self.assertEqual(sn.to_server("/data/users/alice/input.h5ad"),
                         "/data/users/alice/input.h5ad")
        self.assertEqual(sn.to_server("/mnt/reference/genes.tsv"), "/mnt/reference/genes.tsv")
        self.assertEqual(sn.to_server("/data/work/../users/alice/input.h5ad"),
                         "/data/users/alice/input.h5ad")
        for path in ("C:/secret.h5ad", "\x00bad"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "invalid_inspect_path|inspect_path_not_linux_path"):
                    sn.to_server(path)

    def test_contents_paths_map_server_data_paths_to_the_jupyter_contents_root(self):
        self.assertEqual(sn.to_contents("/data/work/a.py"), "work/a.py")
        self.assertEqual(sn.to_contents("/data/users/alice/a.ipynb"),
                         "users/alice/a.ipynb")

    def test_external_absolute_paths_bypass_the_jupyter_contents_root(self):
        self.assertIsNone(sn.contents_path("/mnt/reference/genes.tsv"))
        self.assertEqual(sn.contents_path("/data/users/alice/a.ipynb"),
                         "users/alice/a.ipynb")

    def test_ls_uses_a_server_kernel_for_an_external_absolute_directory(self):
        payload = {"format": "directory", "path": "/mnt/reference", "entries": []}
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "LIST_JSON=" + json.dumps(payload)}}) as run:
            self.assertEqual(sn.list_path("/mnt/reference"), payload)
        self.assertIn("/mnt/reference", run.call_args.args[0])

    def test_external_directory_listing_is_streaming_and_reports_truncation(self):
        self.assertIn("itertools.islice(os.scandir(p), MAX_ENTRIES + 1)", sn._SERVER_LISTING)
        self.assertIn('"truncated": truncated', sn._SERVER_LISTING)
        self.assertNotIn("sorted(os.scandir(p)", sn._SERVER_LISTING)

    def test_cat_uses_the_bounded_server_reader_for_an_external_absolute_file(self):
        expected = {"format": "text", "path": "/mnt/reference/genes.tsv", "head": "gene"}
        with patch.object(sn, "inspect_text", return_value=expected) as inspect_text:
            self.assertEqual(sn.cat_path("/mnt/reference/genes.tsv"), expected)
        inspect_text.assert_called_once_with("/mnt/reference/genes.tsv", max_bytes=sn.DEFAULT_TEXT_PREVIEW_BYTES)

    def test_inspect_uses_an_external_path_without_rejecting_it_before_kernel_execution(self):
        with patch.object(sn, "run_python", return_value={"data": {"stdout": "INSPECT_JSON={\"format\":\"table\"}"}}) as run:
            self.assertEqual(sn.inspect("/data/users/alice/a.csv"), {"format": "table"})
        self.assertIn("/data/users/alice/a.csv", run.call_args.args[0])

    def test_csv_inspection_does_not_interpolate_paths_into_a_shell(self):
        self.assertNotIn("subprocess", sn.TABLE)
        self.assertNotIn("shell=True", sn.TABLE)
        self.assertNotIn("wc -l", sn.TABLE)

    def test_server_inspectors_resolve_paths_and_require_regular_files_without_a_root_allowlist(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET, sn._TEXT_PREVIEW, sn._NOTEBOOK_SUMMARY):
            with self.subTest(source=source[:20]):
                self.assertIn("realpath", source)
                self.assertIn("inspect_path_not_regular_file", source)
                self.assertNotIn("inspect_path_outside_work_root", source)


class FailurePropagationTests(unittest.TestCase):
    def setUp(self):
        self._receipts = tempfile.TemporaryDirectory()
        self.addCleanup(self._receipts.cleanup)
        patcher = patch.object(sn, "_receipts_dir", return_value=self._receipts.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_evaluate_op_raises_for_inner_operation_failure(self):
        with patch.object(sn, "ensure_tab", return_value=7), \
             patch.object(sn, "_call_js", return_value={"ok": False, "error": "kernel_failed"}):
            with self.assertRaisesRegex(sn.Bridge, "kernel_failed"):
                sn.evaluate_op("run_python", {"code": "raise RuntimeError()"})

    def test_run_shell_requires_a_decodable_frame_and_integer_return_code(self):
        with patch.object(sn, "evaluate_op", return_value={"ok": True, "data": {"stdout": "unframed"}}):
            result = sn.run_shell("true")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        self.assertIn("shell_protocol_error", result["stderr"])

    def test_run_shell_marks_nonzero_exit_as_failure(self):
        payload = base64.b64encode(json.dumps({
            "returncode": 7, "stdout": "", "stderr": "bad", "timedOut": False,
        }).encode()).decode()
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value={"ok": True, "data": {
                 "stdout": "__SN_SHELL_fixed__:" + payload}}):
            make_uuid.return_value.hex = "fixed"
            result = sn.run_shell("exit 7")
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)

    def test_runtime_propagates_kernel_error_and_timeout(self):
        source = (Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8"))
        self.assertIn("out.ok = false", source)
        self.assertIn("ok: !!data.ok", source)

    def test_submit_requires_exact_launcher_acknowledgement(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn, "make_job_id", return_value=job_id), \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": "unverified", "stderr": ""}):
            with self.assertRaisesRegex(sn.Bridge, f"submit_unknown: job_id={job_id}"):
                sn.submit("echo work")

    def test_submit_rejects_outer_ack_when_runner_never_reaches_running(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        launch = {"ok": True, "returncode": 0,
                  "stdout": f"__SN_SUBMITTED__={job_id}", "stderr": ""}
        marked = {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
        missing = {"status": None, "missing": {"status": True}, "read_errors": {},
                   "stdout_tail": "", "stderr_tail": ""}
        with patch.object(sn, "make_job_id", return_value=job_id), \
             patch.object(sn, "run_shell", side_effect=[launch, marked]) as run, \
             patch.object(sn, "poll", return_value=missing) as poll, \
             patch.object(sn.time, "monotonic", side_effect=[0.0, 1.0]), \
             patch.object(sn.time, "sleep"):
            with self.assertRaisesRegex(sn.Bridge, "runner_not_ready"):
                sn.submit("echo work", readiness_timeout=0.5)
        poll.assert_called_once_with(job_id, tail=1)
        self.assertIn('"state":"setup_error"', run.call_args_list[-1].args[0])


class DurableRunnerTests(unittest.TestCase):
    def test_controller_job_root_is_fixed_and_not_derived_from_config(self):
        self.assertEqual(sn.JOB_ROOT, "/data/work/agent_jobs")

    def test_launcher_rejects_symlinked_controller_roots(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        root_check = launcher.index("-L /data/work/agent_jobs")
        job_creation = launcher.index("mkdir /data/work/agent_jobs/job_")
        self.assertLess(root_check, job_creation)

    def test_runner_persists_complete_lifecycle_and_artifact_manifest(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        for required in ("manifest.json", "pid", "started_at", "ended_at", "exit_code",
                         "trap finalize EXIT", "trap 'exit 143' TERM", "trap 'exit 130' INT",
                         "artifacts.json", "status.json.tmp.$$"):
            with self.subTest(required=required):
                self.assertIn(required, launcher)
        self.assertLess(launcher.index("artifacts.json.tmp.$$"),
                        launcher.rindex("status.json.tmp.$$"))

    def test_launcher_only_acknowledges_after_runner_readiness_and_marks_timeout(self):
        launcher = sn.build_job_launcher("job_550e8400-e29b-41d4-a716-446655440000", "true")
        self.assertIn("runner.ready", launcher)
        self.assertIn("runner_start_timeout", launcher)
        self.assertIn('"state":"setup_error"', launcher)
        self.assertLess(launcher.index("runner.ready"), launcher.rindex("__SN_SUBMITTED__="))


class BoundedStructuredInspectorsTests(unittest.TestCase):
    def test_all_structured_inspectors_limit_collection_and_value_sizes(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET, sn.RDS_R):
            with self.subTest(source=source[:20]):
                self.assertIn("MAX_ITEMS", source)
                self.assertIn("MAX_VALUE", source)

    def test_inspect_json_parser_rejects_oversized_payloads(self):
        oversized = "INSPECT_JSON=" + json.dumps({"value": "x" * sn.MAX_INSPECT_RESULT_BYTES})
        self.assertEqual(sn._pull_json(oversized), {
            "error": "bounded_summary_too_large",
            "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES,
        })

    def test_notebook_summary_uses_named_cell_and_value_limits(self):
        self.assertIn("MAX_CELLS", sn._NOTEBOOK_SUMMARY)
        self.assertIn("MAX_VALUE", sn._NOTEBOOK_SUMMARY)

    def test_clipped_consumes_only_the_requested_generator_prefix(self):
        namespace = {}
        exec(sn._INSPECT_PRELUDE, namespace)
        consumed = []

        def values():
            for value in range(10000):
                consumed.append(value)
                yield value

        self.assertEqual(namespace["clipped"](values(), 3), ["0", "1", "2"])
        self.assertEqual(consumed, [0, 1, 2])

    def test_oversized_valid_inspection_result_returns_explicit_limit_error(self):
        payload = {"format": "table", "path": "/data/work/a.csv",
                   "head": ["x" * sn.MAX_INSPECT_RESULT_BYTES]}
        response = {"data": {"stdout": "INSPECT_JSON=" + json.dumps(payload)}}
        with patch.object(sn, "run_python", return_value=response):
            result = sn.inspect("/data/work/a.csv")
        self.assertEqual(result, {"error": "bounded_summary_too_large",
                                  "limit_bytes": sn.MAX_INSPECT_RESULT_BYTES})

    def test_every_inspector_emits_one_globally_byte_bounded_json_result(self):
        for source in (sn.H5AD, sn.TABLE, sn.PARQUET,
                       sn._TEXT_PREVIEW, sn._NOTEBOOK_SUMMARY):
            with self.subTest(source=source[:20]):
                self.assertIn("MAX_RESULT_BYTES", source)
                self.assertIn("emit_summary", source)
        self.assertIn("MAX_RESULT_BYTES", sn.RDS_R)
        self.assertIn("bounded_summary_too_large", sn.RDS_R)
        self.assertIn('INSPECT_JSON=', sn.RDS_R)


class BoundedPollProtocolTests(unittest.TestCase):
    def test_poll_uses_byte_caps_base64_and_a_per_call_frame(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": "", "stderr": ""}) as run:
            make_uuid.return_value.hex = "abc123"
            sn.poll(job_id, tail=7)
        command = run.call_args.args[0]
        self.assertIn("__SN_POLL_abc123__", command)
        self.assertIn("head -c", command)
        self.assertIn("tail -c", command)
        self.assertIn("base64", command)
        self.assertIn(str(sn.MAX_STATUS_BYTES + 1), command)
        self.assertIn(str(sn.MAX_LOG_BYTES), command)

    def test_poll_decodes_base64_without_marker_collisions(self):
        job_id = "job_550e8400-e29b-41d4-a716-446655440000"
        status = base64.b64encode(b'{"state":"finished","exit_code":0}').decode()
        stdout = base64.b64encode(b'line\n__SN_STDOUT_END__\nstill data').decode()
        stderr = base64.b64encode(b'').decode()
        raw = "\n".join((f"__SN_POLL_fixed__ status ok {status}",
                          f"__SN_POLL_fixed__ stdout ok {stdout}",
                          f"__SN_POLL_fixed__ stderr ok {stderr}",
                          "__SN_POLL_fixed__ dir ok ",
                          "__SN_POLL_fixed__ live ok b3duZWQ6MTIzNA=="))
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "run_shell", return_value={"ok": True, "returncode": 0,
                                                           "stdout": raw, "stderr": ""}):
            make_uuid.return_value.hex = "fixed"
            result = sn.poll(job_id)
        self.assertEqual(result["stdout_tail"], "line\n__SN_STDOUT_END__\nstill data")
        self.assertEqual(result["status"]["exit_code"], 0)

class PublicReleaseSafetyTests(unittest.TestCase):
    def test_workspace_url_is_canonicalized_and_drops_unrelated_parameters(self):
        url = ("https://www.dcs.cloud/stereonote/?token=SECRET#/notebookEmbed?"
               "projectId=p-123&workspaceId=w-456&token=ALSO_SECRET&extra=x")
        canonical = sn.canonical_workspace_url(url)
        self.assertEqual(canonical,
                         "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p-123&workspaceId=w-456")
        self.assertNotIn("SECRET", canonical)
        self.assertNotIn("extra", canonical)

    def test_workspace_url_rejects_non_dcs_hosts(self):
        with self.assertRaisesRegex(ValueError, "dcs_cloud"):
            sn.canonical_workspace_url(
                "https://evil.example/#/notebookEmbed?projectId=p&workspaceId=w")

    def test_save_url_writes_only_ids_to_user_config(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"SN_CONFIG_DIR": tmp}, clear=False):
            path = Path(tmp) / "config.json"
            with patch.object(sn, "config_path", return_value=str(path)):
                sn.save_url("https://www.dcs.cloud/stereonote/#/notebookEmbed?"
                            "projectId=p123&workspaceId=w456&token=SECRET")
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["project_id"], "p123")
        self.assertEqual(data["workspace_id"], "w456")
        self.assertNotIn("workspace_url", data)
        self.assertNotIn("SECRET", json.dumps(data))

    def test_atomic_write_uses_exclusive_create_private_mode_and_readback(self):
        payload = {"ok": True, "path": "/data/work/out.txt", "bytes": 5, "overwritten": False}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        response = {"data": {"stdout": "__SN_WRITE_fixed__:" + encoded}}
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value=response) as evaluate:
            make_uuid.return_value.hex = "fixed"
            result = sn.write_text("work/out.txt", "hello")
        code = evaluate.call_args.args[1]["code"]
        self.assertIn("os.O_EXCL", code)
        self.assertIn("os.O_NOFOLLOW", code)
        self.assertIn("os.fchmod(_fd, 0o600)", code)
        self.assertIn("write_readback_mismatch", code)
        self.assertEqual(result["data"], payload)

    def test_atomic_write_propagates_file_exists_as_failure(self):
        payload = {"ok": False, "error": "file_exists_use_overwrite", "path": "/data/work/out.txt"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        response = {"data": {"stdout": "__SN_WRITE_fixed__:" + encoded}}
        with patch.object(sn.uuid, "uuid4") as make_uuid, \
             patch.object(sn, "evaluate_op", return_value=response):
            make_uuid.return_value.hex = "fixed"
            with self.assertRaisesRegex(sn.Bridge, "file_exists_use_overwrite"):
                sn.write_text("work/out.txt", "hello")

    def test_write_rejects_paths_outside_work_before_remote_execution(self):
        with patch.object(sn, "evaluate_op") as evaluate:
            with self.assertRaisesRegex(ValueError, "under_work_root"):
                sn.write_text("/data/users/alice/out.txt", "hello")
        evaluate.assert_not_called()

    def test_shell_template_is_disk_backed_and_bounded(self):
        self.assertIn("tempfile.TemporaryFile", sn._SHELL_TMPL)
        self.assertIn("MAX_BYTES", sn._SHELL_TMPL)
        self.assertIn("stdout_truncated", sn._SHELL_TMPL)
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("streamLimitChars", source)
        self.assertIn("655360", source)

    def test_probe_reports_a_lightweight_mode_for_routine_calls(self):
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("probeOk = pr.status_code === 200", source)
        self.assertIn("jupyter_probe_failed", source)
        self.assertIn("if (lite) return r;", source)

    def test_task_base_requires_exact_inner_origin_and_path(self):
        self.assertEqual(sn._task_base("https://inner.dcs.cloud/notebook/st/task-x/lab?token=1"),
                         "/notebook/st/task-x/")
        self.assertIsNone(sn._task_base(
            "https://evil.example/?next=inner.dcs.cloud/notebook/st/x"))
        self.assertIsNone(sn._task_base("http://inner.dcs.cloud/notebook/st/x/"))
        self.assertIsNone(sn._task_base(""))

    def test_bsk_rpc_errors_surface_as_bridge_errors(self):
        reply = json.dumps({"code": "not_found", "message": "session not registered",
                            "exit_code": 1})
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout=reply, stderr="")
        with patch.object(sn, "BSK_BIN", sys.executable), \
             patch.object(sn.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(sn.Bridge, "bsk_not_found"):
                sn.bsk("evaluate", "1")

    def test_bsk_missing_binary_is_reported_before_execution(self):
        with patch.object(sn, "BSK_BIN", os.path.join(tempfile.gettempdir(), "no-such-bsk")):
            with self.assertRaisesRegex(sn.Bridge, "bsk_missing"):
                sn.bsk("session", "list")

    def test_large_payloads_are_chunked_into_bounded_expressions(self):
        expressions = []

        def fake_eval(tab_id, expression, timeout=60, session_id=None):
            expressions.append(expression)
            return {"ok": True, "data": {}}

        payload = {"code": "x" * (sn.MAX_EXPR_CHARS * 2)}
        with patch.object(sn, "_eval_js", side_effect=fake_eval):
            sn._call_js("run_python", payload, 1, 60)
        self.assertGreater(len(expressions), 2)
        self.assertTrue(all(len(item) <= sn.MAX_EXPR_CHARS for item in expressions))

    def test_small_payloads_use_a_single_evaluate_call(self):
        with patch.object(sn, "_eval_js", return_value={"ok": True}) as evaluate:
            sn._call_js("probe", {}, 1, 60)
        self.assertEqual(evaluate.call_count, 1)

    def test_connect_saves_url_only_after_live_connection_succeeds(self):
        url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
        with patch.object(sn, "ensure_tab", return_value=7) as ensure, \
             patch.object(sn, "save_url") as save, patch.object(sn, "_print"), \
             patch.object(sn, "_browser_state", return_value={"session_id": "s1"}):
            self.assertEqual(sn.main(["connect", "--url", url]), 0)
        ensure.assert_called_once_with(force=True, full_probe=True)
        save.assert_called_once_with(url)

    def test_failed_connect_does_not_save_url(self):
        url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
        with patch.object(sn, "ensure_tab", side_effect=sn.Bridge("workspace_startup_timeout")), \
             patch.object(sn, "save_url") as save, patch.object(sn, "_print"):
            self.assertEqual(sn.main(["connect", "--url", url]), 1)
        save.assert_not_called()

    def test_unsafe_artifact_root_is_reported_not_silently_empty(self):
        self.assertIn("unsafe_artifact_root", sn._ARTIFACT_INVENTORY)

    def test_large_artifacts_skip_full_sha256(self):
        self.assertIn("MAX_HASH_BYTES", sn._ARTIFACT_INVENTORY)
        self.assertIn("skipped_large_file", sn._ARTIFACT_INVENTORY)
        self.assertIn(str(sn.MAX_ARTIFACT_HASH_BYTES), sn._ARTIFACT_INVENTORY)

    def test_large_write_and_python_payloads_are_rejected_before_remote_execution(self):
        too_large = "x" * (sn.MAX_WRITE_BYTES + 1)
        with patch.object(sn, "write_text") as write, patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["write", "work/x.txt", "--content", too_large]), 2)
        write.assert_not_called()
        output.assert_called_once_with({"ok": False, "error": "write_content_too_large"})
        with patch.object(sn, "evaluate_op") as evaluate:
            with self.assertRaisesRegex(ValueError, "python_code_too_large"):
                sn.run_python("x" * (sn.MAX_CODE_BYTES + 1))
        evaluate.assert_not_called()

    def test_runtime_bounds_python_errors(self):
        source = Path(sn.RUNTIME_JS_PATH).read_text(encoding="utf-8")
        self.assertIn("boundedError", source)
        self.assertIn("traceback: clipText", source)


@unittest.skipIf(os.name == "nt", "local launcher integration uses POSIX setsid/bash")
class LocalLauncherIntegrationTests(unittest.TestCase):
    def test_launcher_executes_from_requested_cwd_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "jobs"
            cwd = tmp_path / "project"
            cwd.mkdir()
            (cwd / "big.py").write_text(
                "import os, pathlib; pathlib.Path(os.environ['SN_ARTIFACT_DIR'], 'done.txt').write_text('ok')\n",
                encoding="utf-8")
            job_id = "job_550e8400-e29b-41d4-a716-446655440000"
            with patch.object(sn, "JOB_ROOT", str(root)):
                launcher = sn.build_job_launcher(job_id, "python3 big.py", cwd=str(cwd))
            proc = subprocess.run(["bash", "-lc", launcher], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("__SN_SUBMITTED__=" + job_id, proc.stdout)
            status_path = root / job_id / "status.json"
            deadline = time.time() + 10
            status = {}
            while time.time() < deadline:
                if status_path.exists():
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    if status.get("state") == "finished":
                        break
                time.sleep(0.05)
            self.assertEqual(status.get("state"), "finished", status)
            self.assertEqual(status.get("exit_code"), 0, status)
            self.assertEqual((root / job_id / "artifacts" / "done.txt").read_text(), "ok")
            manifest = json.loads((root / job_id / "manifest.json").read_text())
            self.assertEqual(manifest["cwd"], str(cwd))


class ReleaseLayoutTests(unittest.TestCase):
    def test_versions_are_consistent_and_legacy_config_is_not_shipped(self):
        root = Path(sn.SKILL_DIR)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("v" + sn.CONTROLLER_VERSION, skill)
        self.assertIn("## " + sn.CONTROLLER_VERSION, changelog)
        self.assertFalse((root / "config.json").exists())
        self.assertTrue((root / "config.example.json").exists())

    def test_skill_frontmatter_uses_only_official_fields_and_codex_is_explicit(self):
        root = Path(sn.SKILL_DIR)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        openai = (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]
        top_level = [line.split(":", 1)[0] for line in frontmatter.splitlines()
                     if line and not line.startswith((" ", "\t"))]
        self.assertEqual(top_level, ["name", "description"])
        self.assertIn("allow_implicit_invocation: false", openai)
        self.assertIn("$orange-note-skill", openai)
        short = re.search(r'short_description: "([^"]+)"', openai).group(1)
        self.assertGreaterEqual(len(short), 25)
        self.assertLessEqual(len(short), 64)
        self.assertNotIn("mentions\n  StereoNote / DCS / dcs.cloud, a /data/work path", skill)

    def test_public_docs_use_current_codex_and_claude_user_skill_paths(self):
        readme = (Path(sn.SKILL_DIR) / "README.md").read_text(encoding="utf-8")
        self.assertIn("~/.claude/skills/orange-note-skill/", readme)
        self.assertIn("~/.agents/skills/orange-note-skill/", readme)
        self.assertNotIn("~/.codex/skills/orange-note-skill/", readme)


class CliValidationTests(unittest.TestCase):
    def test_run_shell_failure_sets_nonzero_cli_exit(self):
        result = {"ok": False, "returncode": 7, "stdout": "", "stderr": "boom"}
        with patch.object(sn, "run_shell", return_value=result), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["run-shell", "--cmd", "exit 7"]), 1)
        output.assert_called_once_with(result)

    def test_structured_inspect_error_sets_nonzero_cli_exit(self):
        result = {"error": "inspect_failed", "path": "/data/work/x.h5ad"}
        with patch.object(sn, "inspect", return_value=result), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["inspect", "/data/work/x.h5ad"]), 1)
        output.assert_called_once_with(result)

    def test_doctor_failure_sets_nonzero_cli_exit(self):
        with patch.object(sn, "doctor", return_value={"ok": False, "daemon_binary_exists": False}), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["doctor"]), 1)
        output.assert_called_once_with({"ok": False, "daemon_binary_exists": False})

    def test_failed_finished_job_sets_nonzero_poll_cli_exit(self):
        result = {"job_id": "job_x", "status": {"state": "finished", "exit_code": 7},
                  "missing": {"status": False, "stdout": False, "stderr": False},
                  "read_errors": {}, "stdout_tail": "", "stderr_tail": ""}
        with patch.object(sn, "poll", return_value=result), patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["poll", "job_550e8400-e29b-41d4-a716-446655440000"]), 1)
        output.assert_called_once_with(result)

    def test_value_error_is_rendered_as_structured_json(self):
        with patch.object(sn, "poll", side_effect=ValueError("invalid_job_id")), \
             patch.object(sn, "_print") as output:
            self.assertEqual(sn.main(["poll", "bad"]), 2)
        output.assert_called_once_with({"ok": False, "error": "invalid_job_id"})


class ConnectionRecoveryTests(unittest.TestCase):
    url = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"
    task_base = "/notebook/st/task-p/"
    inner = "https://inner.dcs.cloud/notebook/st/task-p/lab"

    def _bsk(self, calls, session_list, tabs, navigate):
        def fake(*args, timeout=120, session=None, tab_id=None):
            calls.append({"args": args, "session": session, "tab_id": tab_id})
            if args[:2] == ("session", "list"):
                return session_list
            if args[:2] == ("session", "start"):
                return {"session_id": "s2"}
            if args[:2] == ("tab", "list"):
                return {"tabs": tabs}
            if args[0] == "navigate":
                return navigate(args[1], tab_id)
            raise AssertionError("unexpected bsk call: %r" % (args,))
        return fake

    def test_session_id_is_reused_while_the_daemon_still_lists_it(self):
        with patch.object(sn, "_browser_state", return_value={"session_id": "s1"}), \
             patch.object(sn, "bsk", return_value=[{"session_id": "s1"}]) as bsk_call:
            self.assertEqual(sn.ensure_session(), "s1")
        self.assertEqual([c.args[:2] for c in bsk_call.call_args_list], [("session", "list")])

    def test_matching_inner_tab_is_reused_without_navigation(self):
        calls = []
        tabs = [{"tab_id": 2, "url": self.inner, "scope": "agent"}]
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={
                 "session_id": "s1", "tab_id": 2, "task_base": self.task_base,
                 "project_id": "p", "workspace_id": "w"}), \
             patch.object(sn, "_probe_tab", return_value=True), \
             patch.object(sn, "_save_browser_state") as save, \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [{"session_id": "s1"}], tabs,
                 lambda url, tab: self.fail("must not navigate"))):
            self.assertEqual(sn.ensure_tab(), 2)
        self.assertFalse(any(c["args"][0] == "navigate" for c in calls))
        save.assert_called_with(tab_id=2)

    def test_missing_session_opens_the_workspace_then_the_jupyter_page(self):
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={}), \
             patch.object(sn, "_eval_js", return_value=self.inner + "?token=x"), \
             patch.object(sn, "_probe_tab", return_value=True), \
             patch.object(sn, "_save_browser_state") as save, \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [], [{"tab_id": 9, "url": "about:blank", "scope": "agent"}],
                 lambda url, tab: {"tab_id": tab, "final_url": url})):
            self.assertEqual(sn.ensure_tab(settle=3), 9)
        navigations = [c for c in calls if c["args"][0] == "navigate"]
        self.assertEqual([c["args"][1] for c in navigations], [self.url, self.inner])
        self.assertEqual([c["tab_id"] for c in navigations], [9, 9])
        save.assert_called()

    def test_foreign_workspace_is_never_reused_from_a_matching_domain(self):
        other = "https://inner.dcs.cloud/notebook/st/task-other/lab"
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={
                 "session_id": "s1", "tab_id": 5, "task_base": self.task_base}), \
             patch.object(sn, "_eval_js", return_value=self.inner), \
             patch.object(sn, "_probe_tab", return_value=True), \
             patch.object(sn, "_save_browser_state"), \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [{"session_id": "s1"}],
                 [{"tab_id": 5, "url": other, "scope": "agent"}],
                 lambda url, tab: {"tab_id": tab, "final_url": url})):
            self.assertEqual(sn.ensure_tab(settle=3), 5)
        navigations = [c for c in calls if c["args"][0] == "navigate"]
        self.assertEqual(len(navigations), 2)

    def test_user_tabs_are_never_reused_for_the_controller(self):
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={"session_id": "s1"}), \
             patch.object(sn, "_eval_js", return_value=self.inner), \
             patch.object(sn, "_probe_tab", return_value=True), \
             patch.object(sn, "_save_browser_state"), \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [{"session_id": "s1"}],
                 [{"tab_id": 5, "url": self.inner, "scope": "user"}],
                 lambda url, tab: {"tab_id": tab if tab is not None else 9,
                                   "final_url": url})):
            self.assertEqual(sn.ensure_tab(settle=3), 9)
        navigations = [c for c in calls if c["args"][0] == "navigate"]
        self.assertEqual(len(navigations), 2)
        self.assertIsNone(navigations[0]["tab_id"])

    def test_missing_configuration_never_opens_a_browser(self):
        with patch.object(sn, "WORKSPACE_URL", ""), \
             patch.object(sn, "WORKSPACE_URL_ERROR", None), patch.object(sn, "bsk") as bsk_call:
            with self.assertRaisesRegex(sn.Bridge, "workspace_not_configured"):
                sn.ensure_tab()
        bsk_call.assert_not_called()

    def test_authentication_failure_is_reported_without_reloading(self):
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={
                 "session_id": "s1", "tab_id": 2, "task_base": self.task_base,
                 "project_id": "p", "workspace_id": "w"}), \
             patch.object(sn, "_probe_tab",
                          side_effect=sn.Bridge("jupyter_auth_required: HTTP 403")) as probe, \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [{"session_id": "s1"}],
                 [{"tab_id": 2, "url": self.inner, "scope": "agent"}],
                 lambda url, tab: self.fail("must not navigate"))):
            with self.assertRaisesRegex(sn.Bridge, "jupyter_auth_required"):
                sn.ensure_tab()
        self.assertEqual(probe.call_count, 1)

    def test_dcs_login_page_is_reported_without_probing_jupyter(self):
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={}), \
             patch.object(sn, "_probe_tab") as probe, patch.object(sn, "_eval_js") as evaluate, \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [], [],
                 lambda url, tab: {"tab_id": 3, "final_url": "https://www.dcs.cloud/#/login"})):
            with self.assertRaisesRegex(sn.Bridge, "dcs_login_required"):
                sn.ensure_tab(settle=0)
        probe.assert_not_called()
        evaluate.assert_not_called()

    def test_startup_wait_is_bounded_and_reports_workspace_timeout(self):
        calls = []
        with patch.object(sn, "WORKSPACE_URL", self.url), \
             patch.object(sn, "_browser_state", return_value={}), \
             patch.object(sn, "_eval_js", return_value=""), \
             patch.object(sn, "_probe_tab") as probe, \
             patch.object(sn, "bsk", side_effect=self._bsk(
                 calls, [], [], lambda url, tab: {"tab_id": 4, "final_url": url})):
            with self.assertRaisesRegex(sn.Bridge, "workspace_startup_timeout"):
                sn.ensure_tab(settle=0)
        self.assertEqual(sum(c["args"][0] == "navigate" for c in calls), 1)
        probe.assert_not_called()

    def test_navigation_race_is_retried_three_times(self):
        def fake(*args, **kwargs):
            if args[:2] == ("session", "list"):
                return [{"session_id": "s1"}]
            raise sn.Bridge('bsk_cdp_failed(evaluate): '
                            '{"message":"Inspected target navigated or closed"}')
        with patch.object(sn, "_browser_state", return_value={"session_id": "s1"}),              patch.object(sn, "bsk", side_effect=fake) as call,              patch.object(sn.time, "sleep"):
            with self.assertRaisesRegex(sn.Bridge, "navigated or closed"):
                sn._eval_js(2, "1")
        self.assertEqual(call.call_count, 4)  # 1 session check + 3 attempts

    def test_close_tolerates_a_session_the_daemon_already_dropped(self):
        with patch.object(sn, "_browser_state", return_value={"session_id": "gone"}),              patch.object(sn, "bsk", side_effect=sn.Bridge("bsk_not_found(session): nope")),              patch.object(sn, "_save_browser_state") as save:
            self.assertEqual(sn.close_tabs(),
                             {"ok": True, "closed": 1, "kernels": None})
        save.assert_called_once_with(session_id=None, tab_id=None, task_base=None)

    def test_operation_timeout_after_dispatch_is_never_replayed(self):
        with patch.object(sn, "ensure_tab", return_value=7), \
             patch.object(sn, "_call_js",
                          side_effect=sn.Bridge("bsk_timeout(evaluate)")) as call:
            with self.assertRaises(sn.Bridge):
                sn.evaluate_op("run_python", {"code": "print(1)"})
        self.assertEqual(call.call_count, 1)



if __name__ == "__main__":
    unittest.main()
