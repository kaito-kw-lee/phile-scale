import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

import adblock as app


def row(url='https://example.org/list', **changes):
    return dict(address=url, enabled=True, groups=[2], comment='keep me', id=1, **changes)


class ListTests(unittest.TestCase):
    def test_merge_preserves_existing_and_defaults_new_off(self):
        result = app.list_choices([dict(name='A', url='https://a'), dict(name='B', url='https://b')],
                                  [row('https://a'), row('https://z')])
        self.assertEqual([r['url'] for r in result], ['https://a', 'https://b', 'https://z'])
        self.assertEqual([r['enabled'] for r in result], [True, False, True])

    def test_save_preserves_metadata_and_encodes_url(self):
        old = [row('https://example.org/list?a=1&b=2')]
        api = Mock(); api.lists.return_value = old
        app.save_lists(api, old, {old[0]['address']: False, 'https://new': True, 'https://unchecked': False})
        calls = api.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('%3Fa%3D1%26b%3D2', calls[0].args[1])
        self.assertEqual(calls[0].args[2], dict(comment='keep me', groups=[2], enabled=False, type='block'))
        self.assertEqual(calls[1].args[2]['groups'], [0])

    def test_conflict_prevents_all_writes(self):
        api = Mock(); api.lists.return_value = [dict(row(), groups=[3])]
        with self.assertRaisesRegex(ValueError, 'changed'):
            app.save_lists(api, [row()], {row()['address']: False})
        api.request.assert_not_called()

    def test_partial_failure_stops_and_reports(self):
        api = Mock(); api.lists.return_value = []
        api.request.side_effect = [{}, ValueError('failed')]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                app.save_lists(api, [], {'https://a': True, 'https://b': True, 'https://c': True})
        self.assertIn('Saved: https://a', err.getvalue())
        self.assertIn('Not attempted: https://c', err.getvalue())
        self.assertEqual(api.request.call_count, 2)

    def test_gravity_error_and_interrupt(self):
        with patch.object(app, 'compose', return_value=subprocess.CompletedProcess([], 5)):
            self.assertEqual(app.gravity_update(), 5)
        with patch.object(app, 'compose', side_effect=KeyboardInterrupt):
            self.assertEqual(app.gravity_update(), 130)

    def test_non_tty_does_not_connect(self):
        with patch.object(app.sys.stdin, 'isatty', return_value=False), patch.object(app, 'PiholeAPI') as api:
            with self.assertRaisesRegex(ValueError, 'terminal'):
                app.manage_lists(app.parser().parse_args(['lists']))
            api.assert_not_called()

    def test_cancel_save_no_changes_and_save_failure(self):
        args = app.parser().parse_args(['lists'])
        for choices, save_error, expected in [(None, None, 0), ({}, None, 1), ({}, ValueError('save failed'), 0)]:
            with self.subTest(choices=choices, error=save_error), \
                 patch.dict(app.os.environ, TERM='xterm'), \
                 patch.object(app.sys.stdin, 'isatty', return_value=True), \
                 patch.object(app.sys.stdout, 'isatty', return_value=True), \
                 patch.object(app, 'PiholeAPI') as api, \
                 patch.object(app, 'recommended_lists', return_value=[]), \
                 patch.object(app, 'select_lists', return_value=choices), \
                 patch.object(app, 'command_lock', contextlib.nullcontext), \
                 patch.object(app, 'save_lists', side_effect=save_error), \
                 patch.object(app, 'gravity_update', return_value=0) as gravity:
                api.return_value.__enter__.return_value.lists.return_value = []
                if save_error:
                    with self.assertRaises(ValueError): app.manage_lists(args)
                else:
                    self.assertEqual(app.manage_lists(args), 0)
                self.assertEqual(gravity.call_count, expected)

    def test_inline_render_navigation_resize_and_cancel(self):
        rows = app.list_choices([dict(url='https://a', name='A', source='https://firebog.net')], [])
        output = io.StringIO()
        sizes = [app.os.terminal_size((100, 24)), app.os.terminal_size((100, 24)), app.os.terminal_size((80, 12))]
        with patch.object(app, 'list_terminal', lambda: contextlib.nullcontext(1)), \
             patch.object(app, 'read_list_key', side_effect=['toggle', None, 'save']), \
             patch.object(app.shutil, 'get_terminal_size', side_effect=sizes), contextlib.redirect_stdout(output):
            self.assertEqual(app.select_lists(rows), {'https://a': True})
        self.assertIn('Recommended by: firebog.net', output.getvalue())
        self.assertIn('[x]', output.getvalue())
        self.assertNotIn('\x1b[2J', output.getvalue())
        self.assertNotIn('1049', output.getvalue())
        with patch.object(app, 'list_terminal', lambda: contextlib.nullcontext(1)), \
             patch.object(app, 'read_list_key', return_value='cancel'), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(app.select_lists(rows))

    def test_source_defaults_overrides_and_user_host(self):
        data = dict(source='https://firebog.net/path', lists=[dict(name='A', url='https://a'),
                    dict(name='B', url='https://b', source='https://other.org')])
        with patch.object(app.Path, 'read_text', return_value=json.dumps(data)):
            rows = app.list_choices(app.recommended_lists(), [row()])
        lines = '\n'.join(app.list_lines(rows, {r['url']: False for r in rows}, 0, (120, 24)))
        self.assertIn('Recommended by: firebog.net', lines)
        self.assertIn('Recommended by: other.org', lines)
        self.assertIn('Download host: example.org', lines)

    def test_controls_unicode_and_scroll(self):
        self.assertEqual(app.terminal_text('\ud55c\uae00abc', 5), '\ud55c\uae00a')
        self.assertNotIn('\x1b', app.terminal_text('bad\x1b[2J\n', 50))
        rows = app.list_choices([dict(name=str(i), url='https://a/' + str(i)) for i in range(30)], [])
        lines = app.list_lines(rows, {r['url']: False for r in rows}, 29, (80, 10))
        self.assertLess(len(lines), 10)
        self.assertIn('> [ ] 29', '\n'.join(lines))

    def test_key_sequences(self):
        for data, key in [(b'\x1b', 'cancel'), (b'\x1b[A', 'up'), (b'\x1bOB', 'down'), (b' ', 'toggle'), (b'\r', 'save')]:
            with self.subTest(data=data), patch.object(app.os, 'read', side_effect=[bytes([c]) for c in data]), \
                 patch.object(app.select, 'select', side_effect=[([1], [], [])] * len(data) + [([], [], [])]):
                self.assertEqual(app.read_list_key(1), key)

    def test_english_child_locale(self):
        with patch.object(app.subprocess, 'run') as run:
            app.run('true')
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')
        self.assertEqual(run.call_args.kwargs['env']['LANGUAGE'], 'C')

    def test_terminal_restoration_on_error(self):
        termios = Mock(TCSANOW=0); termios.tcgetattr.return_value = ['original']
        tty = Mock()
        with patch.dict('sys.modules', termios=termios, tty=tty), \
             patch.object(app.sys.stdin, 'fileno', return_value=1), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                with app.list_terminal():
                    raise RuntimeError('test')
        termios.tcsetattr.assert_called_once_with(1, 0, ['original'])
        self.assertTrue(output.getvalue().endswith('\x1b[?25h'))

    def test_api_auth_and_logout_even_on_failure(self):
        api = app.PiholeAPI()
        api.request = Mock(side_effect=[{'session': {'valid': True, 'sid': 'secret'}}, {}])
        with patch.object(app.Path, 'read_text', return_value='password\n'):
            with self.assertRaises(ValueError):
                with api: raise ValueError('operation failed')
        self.assertEqual(api.request.call_args.args, ('DELETE', '/auth'))
        self.assertIsNone(api.sid)

    def test_http_success_with_processed_error_is_failure(self):
        api = app.PiholeAPI()
        response = Mock()
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock()
        response.read.return_value = json.dumps({'processed': {'errors': [{'error': 'duplicate'}]}}).encode()
        api.opener = Mock(); api.opener.open.return_value = response
        with self.assertRaisesRegex(ValueError, 'rejected'):
            api.request('POST', '/lists?type=block', {})


if __name__ == '__main__':
    unittest.main()
