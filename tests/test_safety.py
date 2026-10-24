"""Behavior checks for cleanup, fresh inspection and uncertain dispatch."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sn_safety', ROOT / 'scripts/sn.py')
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)


class CleanupSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'jobs'
        self.root.mkdir()
        self.sequence = 0

    def job(self, state='finished', age=12, **changes):
        self.sequence += 1
        jid = 'job_550e8400-e29b-41d4-a716-%012d' % self.sequence
        directory = self.root / jid
        directory.mkdir()
        status = {'job_id': jid, 'state': state, 'exit_code': 0,
                  'ended_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - age * 86400))}
        status.update(changes)
        (directory / 'status.json').write_text(json.dumps(status), encoding='utf-8')
        (directory / 'manifest.json').write_text(json.dumps({'job_id': jid, 'schema_version': 2}), encoding='utf-8')
        (directory / 'pid').write_text('99999999', encoding='utf-8')
        (directory / 'artifacts').mkdir()
        (directory / 'artifacts' / 'result.txt').write_text('keep until eligible', encoding='utf-8')
        old = time.time() - age * 86400
        os.utime(directory, (old, old))
        return directory

    def run_prune(self, dry_run=False, kill_error=ProcessLookupError, group_error=ProcessLookupError):
        def execute(command, **kwargs):
            args = shlex.split(command)
            self.assertEqual(args[:2], ['python3', '-c'])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(args[2], {})
            return {'ok': True, 'returncode': 0, 'stdout': output.getvalue(), 'stderr': ''}

        # All deletion targets must resolve inside this test's temporary root.
        self.assertTrue(self.root.is_relative_to(Path(self.tmp.name).resolve()))
        with patch.object(sn, 'JOB_ROOT', str(self.root)), \
             patch.object(sn, 'run_shell', side_effect=execute), \
             patch.object(os, 'kill', side_effect=kill_error), \
             patch.object(os, 'killpg', side_effect=group_error, create=True):
            return sn.prune_jobs(days=7, dry_run=dry_run)

    def test_dry_run_and_delete_select_only_verified_old_finished_jobs(self):
        finished = self.job()
        protected = [self.job('running'), self.job('queued'), self.job('setup_error'),
                     self.job(age=1), self.job(job_id='wrong')]
        unknown = self.root / 'backup'
        unknown.mkdir()
        protected.append(unknown)
        for bad in ('missing', 'invalid', 'oversized', 'non_object', 'bad_manifest', 'no_pid'):
            directory = self.job()
            if bad == 'missing':
                (directory / 'status.json').unlink()
            elif bad == 'invalid':
                (directory / 'status.json').write_text('{broken', encoding='utf-8')
            elif bad == 'oversized':
                (directory / 'status.json').write_text('x' * 5000, encoding='utf-8')
            elif bad == 'non_object':
                (directory / 'status.json').write_text('[]', encoding='utf-8')
            elif bad == 'bad_manifest':
                (directory / 'manifest.json').write_text('{"job_id":"other"}', encoding='utf-8')
            else:
                (directory / 'pid').unlink()
            protected.append(directory)
        planned = self.run_prune(dry_run=True)
        self.assertTrue(planned['ok'], planned)
        self.assertEqual(planned['eligible'], 1)
        self.assertEqual(planned['removed'], 0)
        self.assertEqual(planned['job_ids'], [finished.name])
        self.assertTrue(finished.exists())
        result = self.run_prune()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['removed'], 1)
        self.assertFalse(finished.exists())
        self.assertTrue(all(path.exists() for path in protected))

    def test_live_or_unverifiable_process_is_preserved(self):
        directory = self.job()
        for error in (None, PermissionError):
            with self.subTest(error=error):
                result = self.run_prune(kill_error=error)
                self.assertEqual(result['removed'], 0)
                self.assertTrue(directory.exists())

    def test_orphan_process_group_is_preserved_even_when_runner_is_dead(self):
        directory = self.job()
        result = self.run_prune(group_error=None)
        self.assertEqual(result['removed'], 0)
        self.assertTrue(directory.exists())

    def test_delete_failure_is_reported_and_not_counted_as_success(self):
        directory = self.job()
        with patch('shutil.rmtree', side_effect=PermissionError('fixture denied')):
            result = self.run_prune()
        self.assertFalse(result['ok'])
        self.assertEqual(result['removed'], 0)
        self.assertTrue(directory.exists())

    def test_symlinks_cannot_select_or_delete_an_external_directory(self):
        directory = self.job()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'preserve.txt'
        sentinel.write_text('preserve', encoding='utf-8')
        try:
            (self.root / 'job_550e8400-e29b-41d4-a716-ffffffffffff').symlink_to(outside, target_is_directory=True)
            (directory / 'status.json').unlink()
            (directory / 'status.json').symlink_to(sentinel)
        except OSError as exc:
            self.skipTest('symlinks unavailable: ' + str(exc))
        self.assertEqual(self.run_prune()['removed'], 0)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve')


class DispatchSafety(unittest.TestCase):
    def test_two_sessions_inspect_independently_after_an_old_remote_job_is_pruned(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'SN_CONFIG_DIR': tmp, 'SN_URL': ''}), \
             patch.object(sn, '_wait_for_runner_ready'), patch.object(sn.time, 'sleep'):
            url = 'https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w'
            first_session = sn.create_logical_session(url)
            second_session = sn.create_logical_session(url)
            remote = {}
            dispatches = []
            def launch(command, **kwargs):
                import re
                jid = re.search(r'echo __SN_SUBMITTED__=([^\s]+)', command).group(1)
                remote[jid] = {'session': sn.LOGICAL_SESSION_ID, 'generation': len(dispatches)}
                dispatches.append(jid)
                return {'ok': True, 'returncode': 0, 'stdout': '__SN_SUBMITTED__=' + jid}
            def poll(jid, **kwargs):
                return {'status': {'state': 'finished'}, 'stdout_tail': 'INSPECT_JSON=' + json.dumps(remote[jid])}
            with patch.object(sn, 'run_shell', side_effect=launch), patch.object(sn, 'poll', side_effect=poll):
                with sn.selected_session(first_session):
                    first = sn.inspect_rds('/data/work/same.rds', timeout=1)
                with sn.selected_session(second_session):
                    second = sn.inspect_rds('/data/work/same.rds', timeout=1)
                del remote[dispatches[0]]
                with sn.selected_session(first_session):
                    restored = sn.inspect_rds('/data/work/same.rds', timeout=1)
            self.assertEqual(len(set(dispatches)), 3)
            self.assertEqual((first['session'], second['session'], restored['session']),
                             (first_session, second_session, first_session))
            self.assertEqual(restored['generation'], 2)
            self.assertIn(dispatches[1], remote)

    def test_navigation_error_does_not_replay_a_dispatched_expression(self):
        effects = []
        def execute(*args, **kwargs):
            effects.append('already dispatched')
            raise sn.Bridge('Inspected target navigated or closed')
        with patch.object(sn, 'bsk', side_effect=execute), patch.object(sn.time, 'sleep'):
            with self.assertRaises(sn.Bridge):
                sn._eval_js(7, 'sideEffect()', session_id='fixture')
        self.assertEqual(effects, ['already dispatched'])

    def test_changed_rds_uses_a_fresh_job_and_keeps_general_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sn, '_receipts_dir', return_value=tmp), \
             patch.object(sn, 'WORKSPACE_URL', 'https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w'), \
             patch.object(sn, '_wait_for_runner_ready'), patch.object(sn.time, 'sleep'):
            current = {'n_cells': 12}
            dispatched = {}
            def launch(command, **kwargs):
                import re
                jid = re.search(r'echo __SN_SUBMITTED__=([^\s]+)', command).group(1)
                dispatched[jid] = dict(current)
                return {'ok': True, 'returncode': 0, 'stdout': '__SN_SUBMITTED__=' + jid}
            def poll(jid, **kwargs):
                return {'status': {'state': 'finished', 'exit_code': 0},
                        'stdout_tail': 'INSPECT_JSON=' + json.dumps(dispatched[jid])}
            with patch.object(sn, 'run_shell', side_effect=launch), patch.object(sn, 'poll', side_effect=poll):
                first = sn.inspect_rds('/data/work/same.rds', timeout=1)
                current['n_cells'] = 120
                second = sn.inspect_rds('/data/work/same.rds', timeout=1)
                self.assertEqual((first['n_cells'], second['n_cells']), (12, 120))
                self.assertEqual(len(dispatched), 2)
                a = sn.submit('echo stable', idempotency_key='stable')
                b = sn.submit('echo stable', idempotency_key='stable')
                self.assertEqual(a['job_id'], b['job_id'])
                self.assertTrue(b['reused'])


class WriteSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'result.bin'

    def write(self, data, overwrite=False):
        def execute(op, payload, **kwargs):
            output = io.StringIO()
            native_open = os.open
            def binary_open(path, flags, mode=0o777):
                return native_open(path, flags | getattr(os, 'O_BINARY', 0), mode)
            with contextlib.redirect_stdout(output), patch.object(os, 'fchmod', create=True), \
                 patch.object(os, 'open', side_effect=binary_open):
                exec(payload['code'], {})
            return {'ok': True, 'data': {'stdout': output.getvalue()}}
        with patch.object(sn, 'to_server', return_value=self.target.as_posix()), \
             patch.object(sn, 'evaluate_op', side_effect=execute):
            return sn._write_bytes(self.target.as_posix(), data, overwrite, self.root.as_posix())

    def test_failed_overwrite_preserves_original_file_and_removes_temporary_file(self):
        self.target.write_bytes(b'original')
        with patch.object(os, 'write', side_effect=OSError('fixture disk full')):
            with self.assertRaises(sn.Bridge):
                self.write(b'replacement', overwrite=True)
        self.assertEqual(self.target.read_bytes(), b'original')
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_failed_new_write_does_not_publish_a_partial_destination(self):
        with patch.object(os, 'write', side_effect=OSError('fixture disk full')):
            with self.assertRaises(sn.Bridge):
                self.write(b'new')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_readback_failure_preserves_original_file(self):
        self.target.write_bytes(b'original')
        with patch.object(os, 'read', return_value=b''):
            with self.assertRaisesRegex(sn.Bridge, 'readback_mismatch'):
                self.write(b'replacement', overwrite=True)
        self.assertEqual(self.target.read_bytes(), b'original')
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_successful_overwrite_publishes_complete_binary_data(self):
        self.target.write_bytes(b'original')
        data = bytes(range(256)) * 16
        self.assertTrue(self.write(data, overwrite=True)['ok'])
        self.assertEqual(self.target.read_bytes(), data)
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_a_file_created_during_staging_is_not_overwritten_by_default(self):
        native_write = os.write
        def competing_write(fd, data):
            self.target.write_bytes(b'competitor')
            return native_write(fd, data)
        with patch.object(os, 'write', side_effect=competing_write):
            with self.assertRaisesRegex(sn.Bridge, 'file_exists_use_overwrite'):
                self.write(b'new payload')
        self.assertEqual(self.target.read_bytes(), b'competitor')
        self.assertEqual(list(self.root.iterdir()), [self.target])
