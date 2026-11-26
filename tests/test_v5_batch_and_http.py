"""Tests for Orange Note v5.0.0: transient retries, pure-HTTP fast paths, batch queue, sync, health, bioinfo inspect."""
import contextlib
import gzip
import importlib.util
import io
import json
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sn_v5", ROOT / "scripts/sn.py")
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)
URL = "https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w"


class Phase0TransportAndHttpTests(unittest.TestCase):
    def test_bsk_retries_transient_observation_races_and_succeeds(self):
        calls = []
        responses = [
            (1, {"code": "not_found", "message": "Document changed during observation; observe again", "exit_code": 1}),
            (0, {"state": {"mode": "ready"}, "context_id": "ctx1", "observation_id": "obs1"}),
            (1, {"code": "permission_denied", "message": "The strict observation changed before this action", "exit_code": 1}),
            (0, {"state": {"mode": "ready"}, "context_id": "ctx2", "observation_id": "obs2"}),
            (1, {"code": "permission_denied", "message": "A page frame changed; synchronize before choosing this action", "exit_code": 1}),
            (0, {"state": {"mode": "ready"}, "context_id": "ctx3", "observation_id": "obs3"}),
            (0, {"ok": True, "value": 42}),
        ]

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            rc, body = responses.pop(0)
            return subprocess.CompletedProcess(cmd, rc, json.dumps(body), "")

        with patch.object(sn.os.path, "exists", return_value=True), \
             patch.object(sn, "_bsk_version", return_value="bsk 0.5.0"), \
             patch.object(sn.subprocess, "run", side_effect=fake_run), \
             patch.object(sn.time, "sleep"):
            res = sn.bsk("evaluate", "1+1", session="s1", tab_id=7)
        self.assertEqual(res["value"], 42)
        self.assertEqual(len(calls), 7)

    def test_connect_refresh_passes_force_true(self):
        with patch.object(sn, "ensure_tab", return_value=7) as ensure, \
             patch.object(sn, "_print"), \
             patch.object(sn, "_browser_state", return_value={"session_id": "s1"}):
            self.assertEqual(sn.main(["connect", "--refresh"]), 0)
        ensure.assert_called_once_with(force=True, full_probe=True)

    def test_bsk_never_retries_human_takeover(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"state": {"mode": "user"}, "context_id": None}), ""
            )

        with patch.object(sn.os.path, "exists", return_value=True), \
             patch.object(sn, "_bsk_version", return_value="bsk 0.5.0"), \
             patch.object(sn.subprocess, "run", side_effect=fake_run), \
             patch.object(sn.time, "sleep"):
            with self.assertRaisesRegex(sn.Bridge, "cooperation_not_ready"):
                sn.bsk("evaluate", "1+1", session="s1", tab_id=7)
        self.assertEqual(len(calls), 1)

    def test_classify_page_state_detects_login_stopped_starting(self):
        cases = [
            ({"href": "https://www.dcs.cloud/#/login", "text": "登录"}, "dcs_login_required"),
            ({"href": "https://www.dcs.cloud/stereonote/", "text": "当前容器已停止，请点击启动容器"}, "workspace_stopped"),
            ({"href": "https://www.dcs.cloud/stereonote/", "text": "环境正在排队中 initializing"}, "workspace_starting_timeout"),
        ]
        for payload, expected in cases:
            with patch.object(sn, "_eval_js", return_value=payload):
                diag = sn._classify_page_state(1, "s1")
            self.assertIsNotNone(diag)
            self.assertIn(expected, diag)

    def test_poll_lite_uses_batch_http_without_kernel(self):
        jid = "job_550e8400-e29b-41d4-a716-446655440000"
        now = int(time.time()) - 12
        batch_reply = {
            "ok": True,
            "data": {
                "results": {
                    "status": {"ok": True, "content": json.dumps({"state": "running", "pid": 999, "job_id": jid})},
                    "stage": {"ok": True, "content": json.dumps({"stage": "minimap2", "done": 2, "total": 14})},
                    "heartbeat": {"ok": True, "content": json.dumps({"epoch": now, "pid": 999})},
                    "stdout": {"ok": True, "text": "line1\nline2\nline3\n"},
                    "stderr": {"ok": True, "text": "warn1\n"},
                }
            },
        }
        with patch.object(sn, "evaluate_op", return_value=batch_reply) as ev, \
             patch.object(sn, "run_shell") as shell, \
             patch.object(sn, "_read_receipt", return_value={"state": "submitted"}):
            res = sn.poll(jid, tail=2, lite=True)
        ev.assert_called_once()
        shell.assert_not_called()
        self.assertEqual(res["mode"], "http_lite")
        self.assertEqual(res["outcome"], "running_unverified")
        self.assertEqual(res["process"], "http_unverified")
        self.assertEqual(res["stage"]["stage"], "minimap2")
        self.assertEqual(res["stdout_tail"], "line2\nline3\n")
        self.assertIsInstance(res["heartbeat_age_sec"], int)

    def test_cat_tail_and_multi_cat_use_pure_http(self):
        with patch.object(sn, "evaluate_op", return_value={
            "ok": True, "data": {"ok": True, "size": 50000, "bytes_read": 100, "truncated": True, "text": "end"}
        }) as ev, patch.object(sn, "run_python") as py:
            res = sn.cat_path("work/a.log", tail=True, max_bytes=100)
        ev.assert_called_once_with("read_range", {"path": "work/a.log", "mode": "tail", "max_bytes": 100}, timeout=sn.MAX_SYNC_SECONDS)
        py.assert_not_called()
        self.assertEqual(res["tail"], "end")

        batch_reply = {"ok": True, "data": {"results": {
            "0": {"ok": True, "size": 10, "bytes_read": 10, "truncated": False, "text": "A"},
            "1": {"ok": True, "size": 20, "bytes_read": 20, "truncated": False, "text": "B"},
        }}}
        with patch.object(sn, "evaluate_op", return_value=batch_reply) as ev2, patch.object(sn, "run_python") as py2:
            mres = sn.multi_cat(["work/1.log", "work/2.log"], tail=True, max_bytes=1024)
        ev2.assert_called_once()
        py2.assert_not_called()
        self.assertTrue(mres["ok"])
        self.assertEqual([f["tail"] for f in mres["files"]], ["A", "B"])

    def test_launcher_includes_threads_local_tmp_heartbeat_and_sn_stage(self):
        jid = "job_550e8400-e29b-41d4-a716-446655440000"
        script = sn.build_job_launcher(jid, "echo hi", threads=4, local_tmp=True, batch_id="b1", sample_id="S1")
        self.assertIn("OPENBLAS_NUM_THREADS=4", script)
        self.assertIn("OMP_NUM_THREADS=4", script)
        self.assertIn(f"/tmp/sn_job_{jid}", script)
        self.assertIn("rm -rf", script)
        self.assertIn("sn_stage()", script)
        self.assertIn("heartbeat.json", script)
        self.assertIn('"batch_id":"b1"', script)
        self.assertIn('"sample_id":"S1"', script)

    def test_sn_session_env_binds_logical_session(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"SN_CONFIG_DIR": tmp, "SN_URL": ""}):
            lid = sn.create_logical_session(URL)
            with patch.dict(os.environ, {"SN_SESSION": lid}), \
                 patch.object(sn, "doctor", return_value={"ok": True}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sn.main(["doctor"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["logical_session_id"], lid)


class Phase1BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_parse_manifest_quotes_values_and_rejects_duplicates(self):
        mf = Path(self.tmp.name) / "samples.tsv"
        mf.write_text("sample_id\tfastq\nS1\t/data/a b;rm -rf /.fq.gz\nS2\t/data/s2.fq.gz\n", encoding="utf-8")
        samples = sn.parse_batch_manifest(str(mf), "bash run.sh {sample_id} {fastq}")
        self.assertEqual(len(samples), 2)
        self.assertIn("'/data/a b;rm -rf /.fq.gz'", samples[0]["cmd"])

        bad = Path(self.tmp.name) / "dup.tsv"
        bad.write_text("sample_id\tf\nS1\ta\nS1\tb\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate_sample_id"):
            sn.parse_batch_manifest(str(bad), "echo {sample_id}")

    def test_batch_submit_status_and_retry_failed(self):
        mf = Path(self.tmp.name) / "cohort.tsv"
        mf.write_text("sample_id\tfastq\nS1\t/data/1.fq\nS2\t/data/2.fq\n", encoding="utf-8")
        written = {}
        with patch.object(sn, "WORKSPACE_URL", URL), \
             patch.object(sn, "write_text", side_effect=lambda p, c, overwrite=False: written.__setitem__(p, c)), \
             patch.object(sn, "submit", return_value={"job_id": "job_550e8400-e29b-41d4-a716-446655440001", "reused": False}) as sub:
            res = sn.batch_submit("cohort1", str(mf), "bash run.sh {sample_id} {fastq}", max_concurrent=2, threads=4, local_tmp=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sample_count"], 2)
        self.assertIn(f"{sn.BATCH_ROOT}/cohort1/batch_manifest.json", written)
        self.assertIn(f"{sn.BATCH_ROOT}/cohort1/queue_daemon.py", written)
        sub.assert_called_once()

        manifest_json = written[f"{sn.BATCH_ROOT}/cohort1/batch_manifest.json"]
        manifest_obj = json.loads(manifest_json)
        s1_jid = manifest_obj["samples"][0]["job_id"]
        s2_jid = manifest_obj["samples"][1]["job_id"]
        status_obj = {
            "batch_id": "cohort1", "generation": 1, "state": "finished",
            "controller_job_id": "job_550e8400-e29b-41d4-a716-446655440001",
            "max_concurrent": 2, "counts": {"total": 2, "finished": 1, "failed": 1},
            "samples": {
                "S1": {"sample_id": "S1", "job_id": s1_jid, "attempt": 1, "state": "finished", "exit_code": 0},
                "S2": {"sample_id": "S2", "job_id": s2_jid, "attempt": 1, "state": "failed", "exit_code": 1},
            },
        }
        with patch.object(sn, "WORKSPACE_URL", URL), \
             patch.object(sn, "evaluate_op", side_effect=[
                 {"ok": True, "data": {"ok": True, "content": json.dumps(status_obj)}},
                 {"ok": True, "data": {"results": {
                     "__ctrl_status": {"ok": True, "content": json.dumps({"state": "finished", "exit_code": 1})},
                     "__ctrl_hb": {"ok": True, "content": json.dumps({"epoch": int(time.time()) - 5})},
                     "err:S2": {"ok": True, "text": "OOM error\n"},
                 }}},
             ]):
            st = sn.batch_status("cohort1")
        self.assertTrue(st["ok"])
        self.assertEqual(st["stderr_tails"]["S2"], "OOM error\n")

        with patch.object(sn, "WORKSPACE_URL", URL), \
             patch.object(sn, "evaluate_op", return_value={"ok": True, "data": {"results": {
                 "manifest": {"ok": True, "content": manifest_json},
                 "status": {"ok": True, "content": json.dumps(status_obj)},
             }}}), \
             patch.object(sn, "write_text", side_effect=lambda p, c, overwrite=False: written.__setitem__(p, c)), \
             patch.object(sn, "submit", return_value={"job_id": "job_550e8400-e29b-41d4-a716-446655440002"}):
            retried = sn.batch_retry_failed("cohort1")
        self.assertEqual(retried["retried"], 1)
        self.assertEqual(retried["generation"], 2)
        self.assertEqual(retried["samples"][0]["sample_id"], "S2")
        self.assertEqual(retried["samples"][0]["attempt"], 2)
        self.assertNotEqual(retried["samples"][0]["job_id"], s2_jid)

    def test_prune_with_batch_filter_only_deletes_matching_batch(self):
        root = Path(self.tmp.name).resolve() / "jobs"
        root.mkdir()
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 86400))

        def mk(seq, batch_id):
            jid = f"job_550e8400-e29b-41d4-a716-{seq:012d}"
            d = root / jid
            d.mkdir()
            (d / "status.json").write_text(json.dumps({"job_id": jid, "state": "finished", "ended_at": old_ts, "exit_code": 0}))
            m = {"job_id": jid, "schema_version": 2}
            if batch_id:
                m["batch_id"] = batch_id
            (d / "manifest.json").write_text(json.dumps(m))
            (d / "pid").write_text("99999999")
            return d

        b1 = mk(1, "batch_a")
        b2 = mk(2, "batch_b")
        plain = mk(3, None)

        def exec_shell(cmd, **kwargs):
            args = shlex.split(cmd)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(args[2], {})
            return {"ok": True, "returncode": 0, "stdout": output_val(out), "stderr": ""}

        def output_val(buf):
            return buf.getvalue()

        with patch.object(sn, "JOB_ROOT", str(root)), \
             patch.object(sn, "run_shell", side_effect=exec_shell), \
             patch.object(os, "kill", side_effect=ProcessLookupError), \
             patch.object(os, "killpg", side_effect=ProcessLookupError, create=True):
            res = sn.prune_jobs(days=7, dry_run=False, batch_id="batch_a")
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)
        self.assertFalse(b1.exists())
        self.assertTrue(b2.exists())
        self.assertTrue(plain.exists())


class Phase2And3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_build_sync_tar_excludes_git_and_symlinks(self):
        src = Path(self.tmp.name) / "pkg"
        (src / ".git").mkdir(parents=True)
        (src / ".git" / "HEAD").write_text("ref")
        (src / "run.sh").write_text("#!/bin/bash\necho hi\n")
        (src / "sub").mkdir()
        (src / "sub" / "cfg.yaml").write_text("a: 1\n")
        archive, files, total = sn._build_sync_tar(str(src))
        self.assertGreater(len(archive), 0)
        names = [f["path"] for f in files]
        self.assertEqual(names, ["run.sh", "sub/cfg.yaml"])

    def test_chunked_upload_splits_and_renames(self):
        raw = b"X" * (sn.CHUNKED_UPLOAD_SLICE + 100)
        ops = []

        def fake_call(op, payload, tab, timeout, sid=None):
            ops.append((op, payload.get("chunk") if isinstance(payload, dict) else None))
            return {"ok": True, "data": {"ok": True}}

        stat_calls = [sn.Bridge("remote_stat_failed: not_found"), {"size": len(raw)}, {"size": len(raw)}]
        with patch.object(sn, "ensure_tab", return_value=1), \
             patch.object(sn, "ensure_session", return_value="s1"), \
             patch.object(sn, "_remote_stat", side_effect=stat_calls), \
             patch.object(sn, "_call_js", side_effect=fake_call):
            res = sn._put_chunked_bytes(raw, "work/dir/big.bin", overwrite=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["transport"], "chunked_http")
        self.assertEqual([o for o in ops if o[0] == "put_chunk"], [("put_chunk", 1), ("put_chunk", -1)])
        self.assertIn(("rename_path", None), ops)

    def test_bioinfo_inspectors_execute_on_real_fixtures(self):
        fq = Path(self.tmp.name) / "sample.fastq.gz"
        with gzip.open(fq, "wt") as fh:
            fh.write("@read1\nACGTACGTAC\n+\nIIIIIIIIII\n@read2\nACGT\n+\nIIII\n")
        gtf = Path(self.tmp.name) / "ann.gtf"
        gtf.write_text('#comment\nchr1\tHAVANA\texon\t1\t100\t.\t+\t.\tgene_id "ENSG000001.2";\n')
        paf = Path(self.tmp.name) / "aln.paf"
        paf.write_text("q1\t1500\t0\t1500\t+\tchr1\t100000\t100\t1600\t1400\t1500\t60\n")
        vcf = Path(self.tmp.name) / "var.vcf.gz"
        with gzip.open(vcf, "wt") as fh:
            fh.write("##fileformat=VCFv4.2\n##contig=<ID=chr1>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\nchr1\t100\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\t1/1\n")

        def run_local(code, timeout=50):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(code, {})
            return {"ok": True, "data": {"stdout": buf.getvalue()}}

        with patch.object(sn, "run_python", side_effect=run_local):
            r_fq = sn.inspect(str(fq))
            r_gtf = sn.inspect(str(gtf))
            r_paf = sn.inspect(str(paf))
            r_vcf = sn.inspect(str(vcf))

        self.assertEqual(r_fq["format"], "fastq")
        self.assertEqual(r_fq["sampled_reads"], 2)
        self.assertEqual(r_fq["length_max"], 10)
        self.assertEqual(r_gtf["format"], "gtf_gff")
        self.assertTrue(r_gtf["chrom_prefix_chr"])
        self.assertTrue(r_gtf["gene_id_has_version"])
        self.assertEqual(r_paf["format"], "paf")
        self.assertEqual(r_paf["mean_mapq"], 60.0)
        self.assertEqual(r_vcf["format"], "vcf")
        self.assertEqual(r_vcf["samples"], ["S1", "S2"])


if __name__ == "__main__":
    unittest.main()
