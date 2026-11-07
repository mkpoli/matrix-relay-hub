#!/usr/bin/env python3
"""Exercise cleanup reconciliation without touching Docker."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('irc_proof', Path(__file__).with_name('run.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        lab.resources = {}
        lab.network_id = None
        lab.work = None
        lab.uncertain = False
        self.container = lab.PREFIX + '-synapse'
        self.container_id = 'a' * 64
        self.network_id = 'b' * 64
        self.exists = {'container': True, 'network': True}
        self.label = lab.RUN
        self.removed = []

    def fake_docker(self, *args, **kwargs):
        if args[:2] == ('ps', '-a'):
            flt = args[args.index('--filter') + 1]
            matched = flt in ('name=^/' + self.container + '$', 'id=' + self.container_id)
            return self.container_id if matched and self.exists['container'] else ''
        if args[:2] == ('network', 'ls'):
            flt = args[args.index('--filter') + 1]
            matched = flt in ('name=^' + lab.NETWORK + '$', 'id=' + self.network_id)
            return self.network_id if matched and self.exists['network'] else ''
        if args[0] == 'inspect':
            return json.dumps([{'Id': self.container_id, 'Name': '/' + self.container,
                                'Config': {'Labels': {lab.LABEL: self.label}}}])
        if args[:2] == ('network', 'inspect'):
            return json.dumps([{'Id': self.network_id, 'Name': lab.NETWORK,
                                'Labels': {lab.LABEL: self.label}}])
        if args[:2] == ('rm', '-f'):
            self.removed.append(args[2]); self.exists['container'] = False; return ''
        if args[:2] == ('network', 'rm'):
            self.removed.append(args[2]); self.exists['network'] = False; return ''
        raise AssertionError('unexpected Docker operation')

    def reconcile(self):
        with patch.object(lab, 'docker', side_effect=self.fake_docker), \
             patch.object(lab.signal, 'signal'), patch.object(lab.signal, 'alarm'):
            return lab.cleanup()

    def test_reconciles_uncaptured_ids_but_reports_uncertainty(self):
        lab.uncertain = True
        self.assertFalse(self.reconcile())
        self.assertEqual(self.removed, [self.container_id, self.network_id])

    def test_refuses_mismatched_label(self):
        self.label = 'another-run'
        self.assertFalse(self.reconcile())
        self.assertEqual(self.removed, [])

    def test_mutation_timeout_marks_uncertain(self):
        with patch.object(lab, 'docker', side_effect=subprocess.TimeoutExpired('docker', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                lab.mutate('create')
        self.assertTrue(lab.uncertain)


class DiagnosticTests(unittest.TestCase):
    def test_safe_error_omits_captured_secrets(self):
        error = subprocess.CalledProcessError(23, 'secret-command', output='secret-output', stderr='secret-stderr')
        self.assertEqual(lab.safe_error(error), 'process exit 23')
        self.assertEqual(lab.safe_error(subprocess.TimeoutExpired('secret-command', 1)), 'process timed out')

    def test_partial_progress_only_emits_whitelisted_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(lab.partial_progress(('credential\n' + lab.MATRIX_TO_IRC_PASS + '\nsecret').encode()))
            self.assertFalse(lab.partial_progress('credential ' + lab.MATRIX_TO_IRC_PASS))
        self.assertEqual(out.getvalue(), lab.MATRIX_TO_IRC_PASS + '\n')


if __name__ == '__main__':
    unittest.main()
