#!/usr/bin/env python3
"""Explicit Pi acceptance test; temporarily adds one localhost subscription.

Run as root from a staged repository. Existing subscriptions are preserved.
Uses the installed Compose configuration, staged CLI and staged recommendation catalog.
"""
import functools
import http.server
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('adblock', ROOT / 'adblock.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
catalog = json.loads((ROOT / 'recommended-lists.json').read_text())['lists']


def require(value, label):
    if not value:
        raise RuntimeError('FAIL: ' + label)
    print('PASS: ' + label, flush=True)


def query():
    return subprocess.check_output(['dig', '@127.0.0.1', 'example.net', 'A', '+short'], text=True).strip()


def screen_run(keys, label, resize=False):
    master, slave = pty.openpty()
    import fcntl, termios, struct
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    original_terminal = termios.tcgetattr(slave)
    code = ('import sys,json; sys.path.insert(0,' + repr(str(ROOT)) + '); import adblock as a; '
            'a.recommended_lists=lambda: json.loads(open(' + repr(str(ROOT / 'recommended-lists.json')) +
            ").read()); catalog=a.recommended_lists(); "
            "a.recommended_lists=lambda: [dict(r, source=r.get('source', catalog.get('source', ''))) for r in catalog['lists']]" +
            (" + [dict(name='Scroll test '+str(i),url='https://example.invalid/'+str(i),source='https://firebog.net') for i in range(20)]" if resize else '') +
            "; print('PREVIOUS OUTPUT SENTINEL', flush=True); sys.argv=['adblock','lists']; sys.exit(a.main())")
    process = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                               env=dict(os.environ, TERM='xterm'), start_new_session=True)
    output = b''
    sent = False
    deadline = time.monotonic() + 300
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                output += data
            if not sent and b'Space toggle' in output:
                if resize:
                    os.write(master, b'\x1b[B' * 20)
                    time.sleep(0.3)
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 12, 80, 0, 0))
                    time.sleep(0.3)
                if keys == b'CTRL_C':
                    import signal
                    process.send_signal(signal.SIGINT)
                else:
                    os.write(master, keys)
                sent = True
            if process.poll() is not None and not ready:
                break
        require(sent, label + ' opened inline terminal UI')
        status = process.wait(timeout=10)
        if status:
            # repr keeps captured terminal control sequences out of the caller's terminal.
            print(repr(output[-3000:]), file=sys.stderr)
        require(status == 0, label + ' exited successfully')
        require(termios.tcgetattr(slave) == original_terminal, label + ' restored terminal settings')
        require(b'PREVIOUS OUTPUT SENTINEL' in output and b'\x1b[2J' not in output and
                b'1049' not in output and b'\x1b[3J' not in output, label + ' preserves normal output buffer')
        require(b'Recommended by: firebog.net' in output and (resize or b'Download host:' in output),
                label + ' distinguishes recommendation and download source')
        if resize:
            require(b'Scroll test' in output, 'long-list scrolling and terminal resize')
        return output
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        os.close(master)
        os.close(slave)


def main():
    require(os.geteuid() == 0, 'root execution')
    with app.PiholeAPI() as api:
        original = api.lists()
    require(query() not in ('', '0.0.0.0', '127.0.0.1'), 'test domain resolves before test')
    with tempfile.TemporaryDirectory(prefix='adblock-list-test-') as directory:
        Path(directory, 'hosts.txt').write_text('0.0.0.0 example.net\n')
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/hosts.txt'
        created = False
        try:
            with app.PiholeAPI() as api:
                api.request('POST', '/lists?type=block', dict(address=url, enabled=False, groups=[0],
                            comment='phile-scale temporary acceptance fixture'))
                created = True
                before = api.lists()
            choices = app.list_choices(catalog, before)
            index = next(i for i, r in enumerate(choices) if r['url'] == url)
            screen_run(b'\x1b', 'resize and scrolling cancellation', resize=True)
            screen_run(b'CTRL_C', 'Ctrl+C cancellation')
            output = screen_run(b'\x1b', 'Esc cancellation')
            require(b'Cancelled' in output, 'cancel reports no save')
            with app.PiholeAPI() as api:
                require(app.lists_fingerprint(before) == app.lists_fingerprint(api.lists()), 'cancel preserved lists')
            keys = b'\x1bOB' * index + b' \r'
            output = screen_run(keys, 'enable and Enter')
            require(b'Updating Gravity' in output, 'Enter automatically runs Gravity')
            require(query() == '0.0.0.0', 'selected subscription blocks test domain')
            with app.PiholeAPI() as api:
                enabled = next(r for r in api.lists() if r['address'] == url)
                require(enabled['enabled'] and enabled['groups'] == [0] and
                        enabled['comment'] == 'phile-scale temporary acceptance fixture', 'metadata preserved')
            screen_run(keys, 'disable and Enter')
            require(query() not in ('', '0.0.0.0', '127.0.0.1'), 'unselected subscription no longer blocks domain')
            with app.PiholeAPI() as api:
                require(not next(r for r in api.lists() if r['address'] == url)['enabled'], 'uncheck disables without deletion')
        finally:
            if created:
                with app.PiholeAPI() as api:
                    api.request('DELETE', '/lists/' + urllib.parse.quote(url, safe='') + '?type=block')
                require(app.gravity_update() == 0, 'cleanup Gravity refresh')
            server.shutdown()
            server.server_close()
        with app.PiholeAPI() as api:
            require(app.lists_fingerprint(original) == app.lists_fingerprint(api.lists()), 'original subscriptions restored')
    require(query() not in ('', '0.0.0.0', '127.0.0.1'), 'DNS healthy after cleanup')


if __name__ == '__main__':
    main()
