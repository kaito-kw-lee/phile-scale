#!/usr/bin/env python3
"""Explicit live acceptance test: DNS, lists, backup/restore and denied ingress.

Run as root on the installed Pi over LAN SSH. Temporarily changes example.net
lists, stops the stack for a backup/restore, and creates a private test netns.
Do not run against an existing customized example.net policy.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location('adblock', '/opt/adblock/current/adblock.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def cli(*args):
    subprocess.run(['/usr/local/bin/adblock', *args], check=True)


def query(name='example.net', server='127.0.0.1', tcp=False, port='53'):
    args = ['dig', '@' + server, '-p', port, name, '+short', '+time=3', '+tries=1']
    if tcp:
        args.append('+tcp')
    return subprocess.run(args, text=True, capture_output=True, check=True).stdout.strip()


def require(condition, name):
    if not condition:
        raise RuntimeError('FAIL: ' + name)
    print('PASS: ' + name, flush=True)


def main():
    app.wait_dns()
    checks = app.diagnostics()
    print(json.dumps(checks, indent=2), flush=True)
    require(all(v for k, v in checks.items() if k != 'tailscale_authenticated'), 'core diagnostics and DNSSEC')
    require(bool(query(tcp=True)), 'TCP DNS')
    require(bool(query()), 'UDP DNS')
    require(bool(query(port='5335')), 'Unbound recursive resolver')
    try:
        cli('block', 'add', 'example.net')
        require(query() == '0.0.0.0', 'deny list enforced')
        cli('allow', 'add', 'example.net')
        require(query() not in ('', '0.0.0.0'), 'allow list overrides deny')
    finally:
        cli('allow', 'remove', 'example.net')
        cli('block', 'remove', 'example.net')
    require(query() not in ('', '0.0.0.0'), 'list cleanup')
    backup = app.backup()
    try:
        cli('block', 'add', 'example.net')
        require(query() == '0.0.0.0', 'post-backup change')
        app.restore(backup)
        require(query() not in ('', '0.0.0.0'), 'backup restores previous lists and DNS')
    finally:
        cli('block', 'remove', 'example.net')
    name, host, peer = 'adblock-verification', 'abverify0', 'abverify1'
    r = subprocess.run(['ip', 'netns', 'list'], text=True, capture_output=True, check=True)
    require(name not in r.stdout, 'test namespace name available')
    subprocess.run(['ip', 'netns', 'add', name], check=True)
    try:
        subprocess.run(['ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', peer], check=True)
        subprocess.run(['ip', 'link', 'set', peer, 'netns', name], check=True)
        subprocess.run(['ip', 'addr', 'add', '198.18.255.1/30', 'dev', host], check=True)
        subprocess.run(['ip', 'link', 'set', host, 'up'], check=True)
        subprocess.run(['ip', '-n', name, 'addr', 'add', '198.18.255.2/30', 'dev', peer], check=True)
        subprocess.run(['ip', '-n', name, 'link', 'set', peer, 'up'], check=True)
        for port in ('53', '5335'):
            for tcp in (False, True):
                cmd = ['ip', 'netns', 'exec', name, 'dig', '@198.18.255.1', '-p', port,
                       'example.net', '+time=1', '+tries=1'] + (['+tcp'] if tcp else [])
                result = subprocess.run(cmd, text=True, capture_output=True)
                require(result.returncode != 0, f'denied interface port {port} tcp={tcp}')
    finally:
        subprocess.run(['ip', 'netns', 'delete', name], check=False)
        subprocess.run(['ip', 'link', 'delete', host], check=False, capture_output=True)
    print('LIVE CORE ACCEPTANCE PASSED', flush=True)


if __name__ == '__main__':
    main()
