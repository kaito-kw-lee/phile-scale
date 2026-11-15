import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid
import shutil
import unittest
from unittest.mock import patch

import adblock as app


class NetworkTests(unittest.TestCase):
    def valid(self, **changes):
        values = dict(interface='eth0', address='192.168.10.2/24', gateway='192.168.10.1', cidrs=['192.168.10.0/24'])
        values.update(changes)
        return app.valid_network(**values)

    def test_valid_private_lan(self):
        self.assertEqual(self.valid()['address'], '192.168.10.2/24')

    def test_reject_unsafe_addresses_and_rules(self):
        cases = [dict(address='192.168.10.0/24'), dict(address='192.168.10.255/24'),
                 dict(gateway='192.168.20.1'), dict(gateway='192.168.10.2'),
                 dict(cidrs=['0.0.0.0/0']), dict(cidrs=['192.168.20.0/24']),
                 dict(interface='eth0"; flush ruleset'), dict(address='100.64.0.2/24'),
                 dict(address='203.0.113.2/24', gateway='203.0.113.1', cidrs=['203.0.113.0/24']),
                 dict(cidrs=['192.168.10.2/24'])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.valid(**case)

    def test_firewall_replacement_is_single_transaction(self):
        calls = []
        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(app, 'run', side_effect=runner):
            app.firewall(self.valid())
        self.assertEqual(len(calls), 3)
        rules = calls[-1][1]['input']
        self.assertTrue(rules.startswith('delete table inet adblock\n'))
        self.assertNotIn('flush ruleset', rules)
        self.assertIn('meta nfproto ipv6', rules)
        self.assertLess(rules.index('iifname "eth0"'), rules.index('th dport 53 drop', rules.index('iifname "eth0"')))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parent / ('testdata-' + uuid.uuid4().hex)
        directory.mkdir()
        assert directory.resolve().is_relative_to(Path(__file__).resolve().parent)
        self.addCleanup(shutil.rmtree, directory)
        self.pending = directory / 'pending.json'
        self.config = directory / 'config.json'
        self.patches = [patch.object(app, 'PENDING', self.pending), patch.object(app, 'CONFIG', self.config)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.old = dict(interface='eth0', address='192.168.1.2/24', gateway='192.168.1.1', allow_cidrs=['192.168.1.0/24'])
        self.new = dict(self.old, address='192.168.1.3/24')
        self.state = dict(old=self.old, new=self.new, deadline=200, backup_uuid='backup', new_uuid='new', old_name='LAN', original_auto='yes', old_settings={'ipv4.method': 'auto'}, applied=True)
        app.atomic_json(self.pending, self.state)
        app.atomic_json(self.config, self.new)

    @patch.object(app, 'run')
    @patch.object(app, 'firewall')
    def test_expired_transaction_restores_address_and_policy(self, fw, run):
        with patch.object(app.time, 'time', return_value=201), patch.object(app, 'detect_network', return_value=self.old):
            app.network_rollback()
        self.assertFalse(self.pending.exists())
        self.assertEqual(app.config(), self.old)
        fw.assert_called_once_with(self.old)
        self.assertEqual(run.call_args_list[0].args[:4], ('nmcli', 'connection', 'modify', 'new'))
        self.assertIn(unittest.mock.call('nmcli', 'connection', 'up', 'uuid', 'new', capture=False), run.call_args_list)
        self.assertNotIn(unittest.mock.call('nmcli', 'connection', 'delete', 'uuid', 'new', check=False), run.call_args_list)

    @patch.object(app, 'run')
    def test_early_timer_does_not_change_network(self, run):
        with patch.object(app.time, 'time', return_value=199):
            app.network_rollback()
        run.assert_not_called()
        self.assertTrue(self.pending.exists())

    def test_unhealthy_change_cannot_be_confirmed(self):
        with patch.object(app.time, 'time', return_value=199), patch.object(app, 'dns_ok', return_value=False):
            with self.assertRaises(ValueError):
                app.network_confirm()
        self.assertTrue(self.pending.exists())

    def test_change_cannot_be_confirmed_before_apply_completes(self):
        self.state['applied'] = False
        app.atomic_json(self.pending, self.state)
        with patch.object(app.time, 'time', return_value=199), patch.object(app, 'dns_ok', return_value=True):
            with self.assertRaises(ValueError):
                app.network_confirm()
        self.assertTrue(self.pending.exists())

    def test_failed_recovery_keeps_journal_for_retry(self):
        with patch.object(app.time, 'time', return_value=201), patch.object(app, 'run', side_effect=OSError('link unavailable')):
            with self.assertRaises(OSError):
                app.network_rollback()
        self.assertTrue(self.pending.exists())

    def test_confirm_removes_only_recovery_profile(self):
        with patch.object(app.time, 'time', return_value=199), patch.object(app, 'dns_ok', return_value=True), patch.object(app, 'run') as run:
            app.network_confirm()
        run.assert_called_once_with('nmcli', 'connection', 'delete', 'uuid', 'backup')
        self.assertFalse(self.pending.exists())

    def test_start_waits_for_dns_after_container_start(self):
        events = []
        with patch.object(app, 'firewall', side_effect=lambda c: events.append('firewall')), \
             patch.object(app, 'compose', side_effect=lambda *a, **k: events.append('containers')), \
             patch.object(app, 'wait_dns', side_effect=lambda: events.append('dns_ready')):
            app.start()
        self.assertEqual(events, ['firewall', 'containers', 'dns_ready'])

    def test_start_refreshes_dhcp_address_without_changing_allowed_network(self):
        self.pending.unlink()
        assigned = dict(self.new, address='192.168.1.44/24')
        with patch.object(app, 'detect_network', return_value=assigned), patch.object(app, 'firewall'), \
             patch.object(app, 'compose'), patch.object(app, 'wait_dns'):
            app.start()
        self.assertEqual(app.config()['address'], '192.168.1.44/24')
        self.assertEqual(app.config()['allow_cidrs'], self.old['allow_cidrs'])

    def test_remove_uses_supported_pihole_v6_command(self):
        self.pending.unlink()
        with patch.object(app, 'compose') as compose:
            app.execute(app.parser().parse_args(['block', 'remove', 'example.net']))
        compose.assert_called_once_with('exec', '-T', 'pihole', 'pihole', 'deny', 'remove', 'example.net', capture=False)


class ArchiveTests(unittest.TestCase):
    def archive(self, name, kind=tarfile.REGTYPE):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode='w') as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.SYMTYPE:
                member.linkname = '/etc/shadow'
            archive.addfile(member)
        content.seek(0)
        return tarfile.open(fileobj=content)

    def test_reject_traversal_links_devices_and_unexpected_roots(self):
        for name, kind in [('etc/../../shadow', tarfile.REGTYPE), ('/etc/shadow', tarfile.REGTYPE),
                           ('data/pihole/link', tarfile.SYMTYPE), ('etc/device', tarfile.CHRTYPE),
                           ('data/backups/overwrite', tarfile.REGTYPE), ('unexpected', tarfile.REGTYPE)]:
            with self.subTest(name=name), self.archive(name, kind) as archive:
                with self.assertRaises(ValueError):
                    app.validate_archive(archive)

    def test_accept_normal_data(self):
        with self.archive('data/pihole/gravity.db') as archive:
            app.validate_archive(archive)


if __name__ == '__main__':
    unittest.main()
