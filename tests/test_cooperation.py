import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('orange_cooperation', Path(__file__).resolve().parents[1] / 'scripts/sn.py')
sn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sn)


class CooperationTests(unittest.TestCase):
    def call(self, version, outputs, *args, **kwargs):
        calls = []
        def run(cmd, **options):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(outputs.pop(0)), '')
        with patch.object(sn.os.path, 'exists', return_value=True), patch.object(sn, '_bsk_version', return_value=version), patch.object(sn.subprocess, 'run', side_effect=run):
            result = sn.bsk(*args, **kwargs)
        return result, calls

    def test_04_evaluate_uses_a_fresh_single_use_context(self):
        ready = {'state': {'mode':'ready'}, 'context_id':'fresh'}
        result, calls = self.call('bsk 0.4.0', [ready, {'ok':True, 'value':2}], 'evaluate', '1+1', session='one', tab_id=7)
        self.assertEqual(result['value'], 2)
        self.assertEqual(calls[0][1:3], ['cooperate', 'sync'])
        self.assertIn('--settle', calls[0])
        self.assertEqual(calls[1][calls[1].index('--context') + 1], 'fresh')
        self.assertEqual(len(calls), 2)

    def test_03_retains_existing_call(self):
        result, calls = self.call('bsk 0.3.3', [{'ok':True}], 'evaluate', '1', session='one', tab_id=7)
        self.assertTrue(result['ok'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--context', calls[0])

    def test_unready_never_dispatches_execution(self):
        outputs = [{'state': {'mode':'user'}, 'context_id':None}]
        with self.assertRaisesRegex(sn.Bridge, 'cooperation_not_ready'):
            self.call('bsk 0.4.0', outputs, 'evaluate', 'danger()', session='one', tab_id=7)
        self.assertEqual(outputs, [])

    def test_blank_owned_tab_initial_navigation_needs_no_sync(self):
        tabs = {'tabs':[{'scope':'agent', 'tab_id':7, 'url':'about:blank'}]}
        _, calls = self.call('bsk 0.4.0', [tabs, {'tab_id':7}], 'navigate', 'https://example.test', session='one', tab_id=7)
        self.assertEqual(calls[0][1:3], ['tab','list'])
        self.assertEqual(calls[1][1], 'navigate')
        self.assertNotIn('--context', calls[1])

    def test_existing_tab_navigation_needs_context(self):
        tabs = {'tabs':[{'scope':'agent', 'tab_id':7, 'url':'https://example.test'}]}
        ready = {'state':{'mode':'ready'}, 'context_id':'next'}
        _, calls = self.call('bsk 0.4.0', [tabs, ready, {'tab_id':7}], 'navigate', 'https://example.test/next', session='one', tab_id=7)
        self.assertEqual(calls[1][1:3], ['cooperate','sync'])
        self.assertEqual(calls[2][calls[2].index('--context') + 1], 'next')

    def test_sync_errors_are_not_hidden_as_startup_timeout(self):
        with patch.object(sn, 'ensure_session', return_value='s'), patch.object(sn, '_browser_state', return_value={}), patch.object(sn, '_agent_tabs', return_value=[{'tab_id':1}]), patch.object(sn, 'bsk', side_effect=[{'tab_id':1}, sn.Bridge('cooperation_not_ready: user')]), patch.object(sn, 'WORKSPACE_URL', 'https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w'):
            with self.assertRaisesRegex(sn.Bridge, 'cooperation_not_ready'):
                sn.ensure_tab(settle=1)

    def test_version_unknown_fails_closed_before_action(self):
        with self.assertRaisesRegex(sn.Bridge, 'bsk_version_unknown'):
            self.call('', [], 'evaluate', 'danger()', session='one', tab_id=7)

    def test_04_missing_tab_never_navigates_an_unverified_page(self):
        with self.assertRaisesRegex(sn.Bridge, 'agent_tab_unverified'):
            self.call('bsk 0.4.0', [], 'navigate', 'https://example.test', session='one')

    def test_action_error_is_not_replayed(self):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1:3] == ['cooperate', 'sync']:
                body = {'state': {'mode':'ready'}, 'context_id':'one-use'}
                return subprocess.CompletedProcess(cmd, 0, json.dumps(body), '')
            body = {'code':'timeout', 'message':'unknown outcome', 'exit_code':1}
            return subprocess.CompletedProcess(cmd, 1, json.dumps(body), '')
        with patch.object(sn.os.path, 'exists', return_value=True), patch.object(sn, '_bsk_version', return_value='bsk 0.4.0'), patch.object(sn.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(sn.Bridge, 'unknown outcome'):
                sn.bsk('evaluate', 'submit()', session='one', tab_id=7)
        self.assertEqual([c[1] for c in calls], ['cooperate', 'evaluate'])

    def test_deep_doctor_reports_coordination_issue_without_refresh_advice(self):
        with patch.object(sn, '_bsk_version', return_value='bsk 0.4.0'), patch.object(sn, 'bsk', side_effect=lambda *a, **k: [{'session_id':'s'}] if a[:2] == ('session','list') else [{'browser_name':'edge'}]), patch.object(sn, '_browser_state', return_value={'session_id':'s','task_base':'/x/'}), patch.object(sn, '_agent_tabs', return_value=[{'url':sn.INNER_ORIGIN + '/x/','tab_id':1}]), patch.object(sn, '_probe_tab', side_effect=sn.Bridge('cooperation_not_ready: user')), patch.object(sn, 'WORKSPACE_URL', 'https://www.dcs.cloud/stereonote/#/notebookEmbed?projectId=p&workspaceId=w'):
            info = sn.doctor(deep=True)
        self.assertFalse(info['ok'])
        self.assertEqual(info['layers'][-1]['layer'], 'jupyter')
        self.assertNotIn('connect', info['layers'][-1]['action'])
        self.assertIn('cooperat', info['layers'][-1]['action'])

    def test_stat_uses_followed_target_when_contents_reports_link_size(self):
        remote = {'ok':True, 'data':{'type':'file','size':10,'last_modified':'link','range_size':524288}}
        followed = {'size':524288, 'last_modified':'target-fingerprint'}
        with patch.object(sn, '_call_js', return_value=remote), patch.object(sn, '_resolved_link_stat', return_value=followed, create=True) as resolve:
            result = sn._remote_stat('work/link.bin', 'session', 7)
        self.assertEqual(result, followed)
        resolve.assert_called_once_with('work/link.bin', 524288)

    def test_link_stat_fails_closed_if_target_changes_or_is_not_link(self):
        for data in ({'is_link':True,'size':8,'fingerprint':[1]}, {'is_link':False,'size':9,'fingerprint':[1]}):
            with patch.object(sn, 'run_python', return_value={'data':{'stdout':json.dumps(data)}}):
                with self.assertRaisesRegex(sn.Bridge, 'remote_link_size_changed_or_unverified'):
                    sn._resolved_link_stat('work/link.bin', 9)
