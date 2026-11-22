#!/usr/bin/env python3
"""Raspberry Pi DNS appliance. All subprocess arguments are passed without a shell."""
import argparse
import contextlib
import datetime
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import select
import unicodedata
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ETC = Path('/etc/adblock')
DATA = Path('/var/lib/adblock')
APP = Path('/opt/adblock/current')
CONFIG = ETC / 'config.json'
PENDING = DATA / 'network-pending.json'
SERVICES = ('unbound', 'pihole', 'tailscale')


class PiholeAPI:
    """Local v6 API session; never expose credentials in URLs or diagnostics."""
    def __init__(self):
        self.sid = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method, path, payload=None):
        headers = {'Content-Type': 'application/json'}
        if self.sid:
            headers['X-FTL-SID'] = self.sid
        req = urllib.request.Request('http://127.0.0.1:8080/api' + path,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=10) as response:
                raw = response.read()
            result = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raise ValueError(f'Pi-hole API HTTP {error.code}; check service and authentication') from None
        except (OSError, ValueError) as error:
            raise ValueError('Pi-hole API connection failed or returned invalid JSON') from None
        if not isinstance(result, dict) or result.get('error') or (result.get('processed') or {}).get('errors'):
            raise ValueError('Pi-hole API rejected the operation')
        return result

    def __enter__(self):
        session = self.request('POST', '/auth', {'password': (ETC / 'webpassword').read_text().strip()}).get('session', {})
        if not session.get('valid'):
            raise ValueError('Pi-hole authentication failed; check webpassword and any 2FA configuration')
        self.sid = session.get('sid')
        return self

    def __exit__(self, *args):
        if self.sid:
            try:
                self.request('DELETE', '/auth')
            except ValueError:
                print('Warning: could not close Pi-hole API session; it will expire.', file=sys.stderr)
            finally:
                self.sid = None

    def lists(self):
        rows = self.request('GET', '/lists?type=block').get('lists')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or
                not all(k in r for k in ('address', 'enabled', 'groups', 'comment')) for r in rows):
            raise ValueError('Unexpected Pi-hole list response')
        return [r for r in rows if r.get('type', 'block') == 'block']


def recommended_lists():
    catalog = json.loads((APP / 'recommended-lists.json').read_text())
    rows = [dict(row, source=row.get('source', catalog.get('source', ''))) for row in catalog['lists']]
    seen = set()
    for row in rows:
        url = urllib.parse.urlsplit(row['url'])
        if url.scheme not in ('http', 'https') or not url.hostname or row['url'] in seen:
            raise ValueError('Invalid or duplicate recommended list URL')
        seen.add(row['url'])
    return rows


def list_choices(recommended, existing):
    registered = {r['address']: r for r in existing}
    rows = []
    for entry in recommended:
        old = registered.pop(entry['url'], None)
        rows.append(dict(url=entry['url'], name=entry['name'], recommended=True, source=entry.get('source', ''),
                         registered=old is not None, enabled=bool(old and old['enabled'])))
    for url, old in sorted(registered.items()):
        rows.append(dict(url=url, name=url, recommended=False, source='', registered=True, enabled=bool(old['enabled'])))
    return rows


def terminal_text(text, width):
    """Sanitize terminal controls and truncate by display cells, not code points."""
    result, used = [], 0
    for char in str(text):
        if not char.isprintable():
            char = '?'
        cells = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)
        if used + cells > max(0, width):
            break
        result.append(char)
        used += cells
    return ''.join(result)


def list_lines(rows, selected, cursor, size):
    width, height = size
    if width < 25 or height < 7:
        return [terminal_text('Resize terminal; Esc cancels.', width - 1)]
    visible = max(1, height - 5)
    start = max(0, cursor - visible + 1)
    lines = ['Select block lists']
    for index in range(start, min(len(rows), start + visible)):
        row = rows[index]
        host = urllib.parse.urlsplit(row['source'] if row['recommended'] else row['url']).hostname
        source = ('Recommended by: ' if row['recommended'] else 'Download host: ') + (host or 'unknown')
        prefix = f"{'>' if index == cursor else ' '} [{'x' if selected[row['url']] else ' '}] "
        # Reserve room for attribution even when the list name is a long URL.
        name_width = max(4, width - len(prefix) - len(source) - 3)
        lines.append(prefix + terminal_text(row['name'], name_width) + '  ' + source)
    if not rows:
        lines.append('No block lists available.')
    lines.append('Download: ' + (rows[cursor]['url'] if rows else '(none)'))
    lines.append('Up/Down move | Space toggle | Enter save + update | Esc cancel')
    return [terminal_text(line, width - 1) for line in lines]


def read_list_key(fd):
    if not select.select([fd], [], [], 0.2)[0]:
        return None
    char = os.read(fd, 1)
    if char == b'':
        return 'cancel'
    if char == b'\x1b':
        sequence = char
        # A standalone Escape cancels; CSI and SS3 arrows are navigation.
        while len(sequence) < 16 and select.select([fd], [], [], 0.08)[0]:
            sequence += os.read(fd, 1)
            if len(sequence) >= 3 and 0x40 <= sequence[-1] <= 0x7e:
                break
        if sequence == b'\x1b':
            return 'cancel'
        return {b'\x1b[A': 'up', b'\x1bOA': 'up', b'\x1b[B': 'down', b'\x1bOB': 'down'}.get(sequence)
    return {b'\x03': 'cancel', b'\r': 'save', b'\n': 'save', b' ': 'toggle'}.get(char)


@contextlib.contextmanager
def list_terminal():
    import termios
    import tty
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        sys.stdout.write('\x1b[?25l')
        sys.stdout.flush()
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, previous)
        sys.stdout.write('\x1b[?25h')
        sys.stdout.flush()


def select_lists(rows):
    """Render only our own lines in the normal terminal buffer."""
    selected = {r['url']: r['enabled'] for r in rows}
    cursor = 0
    previous_lines, previous_size = [], None
    with list_terminal() as fd:
        while True:
            size = shutil.get_terminal_size()
            lines = list_lines(rows, selected, cursor, size)
            if lines != previous_lines or size != previous_size:
                if previous_lines and size == previous_size:
                    sys.stdout.write(f'\x1b[{len(previous_lines)}A')
                # After resize the old lines may have reflowed: append safely.
                for line in lines:
                    sys.stdout.write('\r\x1b[2K' + line + '\n')
                sys.stdout.flush()
                previous_lines, previous_size = lines, size
            key = read_list_key(fd)
            if key == 'cancel':
                return None
            if key == 'save':
                return selected
            if rows:
                if key == 'up':
                    cursor = max(0, cursor - 1)
                elif key == 'down':
                    cursor = min(len(rows) - 1, cursor + 1)
                elif key == 'toggle':
                    url = rows[cursor]['url']
                    selected[url] = not selected[url]


def lists_fingerprint(rows):
    # Ignore download statistics which can change during a scheduled gravity run.
    return sorted((r['address'], json.dumps({k: r.get(k) for k in
                   ('id', 'type', 'enabled', 'groups', 'comment', 'date_modified')}, sort_keys=True)) for r in rows)


def save_lists(api, original, choices):
    if lists_fingerprint(api.lists()) != lists_fingerprint(original):
        raise ValueError('Lists changed while selecting; nothing saved. Run adblock lists again.')
    old = {r['address']: r for r in original}
    changes = [(url, enabled) for url, enabled in choices.items()
               if (url in old and bool(old[url]['enabled']) != enabled) or (url not in old and enabled)]
    saved = []
    for index, (url, enabled) in enumerate(changes):
        try:
            if url in old:
                payload = {k: old[url][k] for k in ('comment', 'groups')}
                payload.update(enabled=enabled, type='block')
                api.request('PUT', '/lists/' + urllib.parse.quote(url, safe='') + '?type=block', payload)
            else:
                api.request('POST', '/lists?type=block', dict(address=url, enabled=True,
                            groups=[0], comment='phile-scale recommended list'))
            saved.append(url)
        except (ValueError, KeyboardInterrupt):
            print('Saved: ' + (', '.join(saved) or '(none)'), file=sys.stderr)
            print('Failed or uncertain: ' + url, file=sys.stderr)
            print('Not attempted: ' + (', '.join(u for u, _ in changes[index + 1:]) or '(none)'), file=sys.stderr)
            raise ValueError('List save incomplete; Gravity was not run. Reopen adblock lists to review and retry.') from None
    print(f'Saved selection ({len(saved)} changed lists).')


def gravity_update():
    print('Updating Gravity; review the output for individual download warnings.', flush=True)
    try:
        result = compose('exec', '-T', '-e', 'LC_ALL=C', '-e', 'LANGUAGE=C', 'pihole', 'pihole', '-g', capture=False, check=False)
    except KeyboardInterrupt:
        print('Update interrupted; saved choices retained. Retry: sudo adblock gravity update', file=sys.stderr)
        return 130
    except OSError:
        print('Update could not start; saved choices retained. Retry: sudo adblock gravity update', file=sys.stderr)
        return 1
    if result.returncode:
        print('Update failed; saved choices retained. Retry: sudo adblock gravity update', file=sys.stderr)
    return result.returncode


def manage_lists(args):
    if args.action == 'list':
        with PiholeAPI() as api:
            rows = api.lists()
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(f"[{'x' if row['enabled'] else ' '}] {row['address']} groups={row['groups']} {row['comment'] or ''}")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Interactive terminal required; use adblock lists list --json for read-only output')
    if os.environ.get('TERM', '') in ('', 'dumb'):
        raise ValueError('ANSI terminal required; use adblock lists list --json for read-only output')
    with PiholeAPI() as api:
        original = api.lists()
    rows = list_choices(recommended_lists(), original)
    try:
        choices = select_lists(rows)
    except KeyboardInterrupt:
        choices = None
    if choices is None:
        print('Cancelled; no changes saved.')
        return 0
    # Authenticate again after interactive input so an idle session cannot expire while selecting.
    with command_lock():
        with PiholeAPI() as api:
            save_lists(api, original, choices)
        return gravity_update()


def run(*args, check=True, capture=True, input=None):
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          capture_output=capture, input=input, env=dict(os.environ, LC_ALL='C', LANGUAGE='C'))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def config():
    if not CONFIG.exists():
        raise ValueError('Not installed. Run adblock install first.')
    return json.loads(CONFIG.read_text())


def compose(*args, **kwargs):
    return run('docker', 'compose', '--ansi', 'never', '--project-name', 'adblock', '--env-file',
               ETC / 'images.env', '-f', APP / 'compose.yaml', *args, **kwargs)


def valid_network(interface, address, gateway, cidrs):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('Invalid interface name')
    addr = ipaddress.IPv4Interface(address)
    gw = ipaddress.IPv4Address(gateway)
    if addr.network.prefixlen > 30 or addr.ip in (addr.network.network_address, addr.network.broadcast_address):
        raise ValueError('Use a usable IPv4 host address with prefix /1 through /30')
    if gw not in addr.network or gw == addr.ip or gw in (addr.network.network_address, addr.network.broadcast_address):
        raise ValueError('Gateway must be another usable host in the LAN subnet')
    private = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not any(addr.network.subnet_of(n) for n in private):
        raise ValueError('Use a private LAN address')
    allowed = [str(ipaddress.IPv4Network(c, strict=True)) for c in cidrs]
    if not allowed or any(ipaddress.ip_network(c).prefixlen == 0 for c in allowed):
        raise ValueError('At least one explicit LAN CIDR required; /0 is forbidden')
    if not any(addr.ip in ipaddress.ip_network(c) for c in allowed):
        raise ValueError('LAN address must be inside an allowed CIDR')
    ts = ipaddress.ip_network('100.64.0.0/10')
    if addr.network.overlaps(ts):
        raise ValueError('LAN overlaps Tailscale range')
    return dict(interface=interface, address=str(addr), gateway=str(gw), allow_cidrs=allowed)


def firewall_text(c, exists=False):
    # Priority -10 rejects forbidden traffic before Tailscale or other filter accepts.
    prefix = 'delete table inet adblock\n' if exists else ''
    networks = ', '.join(c['allow_cidrs'])
    return prefix + '''table inet adblock {
 chain dns_input {
  type filter hook input priority -10; policy accept;
  iifname "lo" accept
  meta nfproto ipv6 meta l4proto { tcp, udp } th dport 53 drop
  iifname "tailscale0" meta nfproto ipv4 meta l4proto { tcp, udp } th dport 53 accept
  iifname "%s" ip saddr { %s } meta l4proto { tcp, udp } th dport 53 accept
  meta l4proto { tcp, udp } th dport 53 drop
  iifname != "lo" meta l4proto { tcp, udp } th dport 5335 drop
 }
}
''' % (c['interface'], networks)


def firewall(c):
    valid_network(c['interface'], c['address'], c['gateway'], c['allow_cidrs'])
    exists = run('nft', 'list', 'table', 'inet', 'adblock', check=False).returncode == 0
    rules = firewall_text(c, exists)
    run('nft', '-c', '-f', '-', input=rules)
    run('nft', '-f', '-', input=rules)


def dns_ok():
    r = run('dig', '@127.0.0.1', 'example.com', '+time=3', '+tries=1', check=False)
    return r.returncode == 0 and 'status: NOERROR' in r.stdout and bool(re.search(r'ANSWER: [1-9]', r.stdout))


def start():
    current = config()
    if not PENDING.exists():
        actual = detect_network()
        if actual['interface'] == current['interface']:
            current = valid_network(current['interface'], actual['address'], actual['gateway'], current['allow_cidrs'])
            atomic_json(CONFIG, current)
    firewall(current)
    compose('up', '-d', capture=False)
    wait_dns()


def detect_network():
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'default').stdout)
    routes = [r for r in routes if r.get('gateway') and r.get('dev') != 'tailscale0']
    if not routes:
        raise ValueError('No IPv4 default LAN route found')
    route = min(routes, key=lambda r: r.get('metric', 0))
    addresses = json.loads(run('ip', '-j', '-4', 'addr', 'show', 'dev', route['dev']).stdout)
    addr = next(a for a in addresses[0]['addr_info'] if a['scope'] == 'global')
    value = f"{addr['local']}/{addr['prefixlen']}"
    return valid_network(route['dev'], value, route['gateway'], [str(ipaddress.ip_interface(value).network)])


def preflight(install=False):
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64', 'arm64'):
        raise ValueError('Requires Linux ARM64 (aarch64)')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    release = {k: v.strip('"') for k, v in release.items()}
    if release.get('ID') not in ('debian', 'raspbian') or int(release.get('VERSION_ID', '0').split('.')[0]) < 13:
        raise ValueError('Requires Debian/Raspberry Pi OS 13 or newer')
    for cmd in ('nmcli', 'ip', 'systemctl'):
        if not shutil.which(cmd):
            raise ValueError(f'Missing prerequisite: {cmd}')
    run('systemctl', 'is-active', '--quiet', 'NetworkManager')
    if not Path('/dev/net/tun').exists():
        raise ValueError('/dev/net/tun is unavailable')
    if shutil.disk_usage('/var').free < 2 * 1024**3:
        raise ValueError('At least 2 GiB free space required')
    if run('timedatectl', 'show', '-p', 'NTPSynchronized', '--value').stdout.strip() != 'yes':
        raise ValueError('Synchronize system clock before installing (DNSSEC requires correct time)')
    if install and not CONFIG.exists():
        listeners = run('ss', '-H', '-lntu', 'sport = :53').stdout.strip()
        if listeners:
            raise ValueError('Port 53 already in use; resolve the conflict before installing:\n' + listeners)
    return release


def resolve_image(ref):
    # Resolve a chosen tag once; all subsequent starts use its immutable digest.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]*', ref):
        raise ValueError('Invalid Docker image reference')
    run('docker', 'pull', ref, capture=False)
    details = json.loads(run('docker', 'image', 'inspect', ref).stdout)[0]
    if details['Architecture'] != 'arm64':
        raise ValueError(f'{ref} is not ARM64')
    if not details.get('RepoDigests'):
        raise ValueError(f'No immutable digest returned for {ref}')
    return details['RepoDigests'][0]


def prepare_images(pihole, tailscale, base):
    pi = resolve_image(pihole)
    ts = resolve_image(tailscale)
    debian = resolve_image(base)
    tag = 'adblock-unbound:build-' + str(time.time_ns())
    run('docker', 'build', '--build-arg', 'BASE_IMAGE=' + debian, '-t', tag, APP / 'unbound', capture=False)
    unbound = json.loads(run('docker', 'image', 'inspect', tag).stdout)[0]['Id']
    return {'PIHOLE_IMAGE': pi, 'TAILSCALE_IMAGE': ts, 'UNBOUND_IMAGE': unbound, 'BASE_IMAGE': debian}


def write_images(images):
    path = ETC / 'images.env'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(''.join(f'{k}={v}\n' for k, v in images.items()))
    tmp.chmod(0o600)
    tmp.replace(path)


def install(args):
    preflight(install=True)
    if CONFIG.exists():
        print('Already installed; validating configuration and starting existing pinned images.')
        register_runtime(Path(__file__).resolve().parent)
        compose('config', '--quiet')
        start()
        return
    if not all((args.pihole_image, args.tailscale_image)):
        raise ValueError('Provide --pihole-image and --tailscale-image with reviewed version tags or digests')
    network = detect_network()
    if not shutil.which('docker') or run('docker', 'compose', 'version', check=False).returncode:
        # Debian packages avoid configuring an unverified third-party apt source on future OS versions.
        run('apt-get', 'update', capture=False)
        packages = ['docker-compose'] if shutil.which('docker') else ['docker.io', 'docker-compose']
        run('apt-get', 'install', '-y', *packages, capture=False)
    run('apt-get', 'update', capture=False)
    run('apt-get', 'install', '-y', 'nftables', 'dnsutils', 'iputils-arping', capture=False)
    run('systemctl', 'enable', '--now', 'docker', capture=False)
    source = Path(__file__).resolve().parent
    APP.mkdir(parents=True, exist_ok=True)
    for name in ('compose.yaml', 'adblock.py', 'recommended-lists.json', 'unbound', 'systemd'):
        src, dst = source / name, APP / name
        if src.resolve() != dst.resolve():
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    ETC.mkdir(parents=True, exist_ok=True)
    ETC.chmod(0o700)
    DATA.mkdir(parents=True, exist_ok=True)
    DATA.chmod(0o700)
    for name in (*SERVICES, 'backups', 'profiles'):
        (DATA / name).mkdir(exist_ok=True)
    shutil.copy2(APP / 'unbound/unbound.conf', ETC / 'unbound.conf')
    password = ETC / 'webpassword'
    password.write_text(secrets.token_urlsafe(32) + '\n')
    password.chmod(0o600)
    write_images(prepare_images(args.pihole_image, args.tailscale_image, args.base_image))
    compose('config', '--quiet')
    atomic_json(CONFIG, network)
    register_runtime(source)
    print('Installed. Reserve the LAN IP in your router; run adblock tailscale login.')


def register_runtime(source):
    if source != APP and (source / 'compose.yaml').exists():
        for name in ('compose.yaml', 'adblock.py', 'recommended-lists.json', 'unbound', 'systemd'):
            if (source / name).is_dir():
                shutil.copytree(source / name, APP / name, dirs_exist_ok=True)
            else:
                shutil.copy2(source / name, APP / name)
    binary = Path('/usr/local/bin/adblock')
    shutil.copy2(APP / 'adblock.py', binary)
    binary.chmod(0o755)
    for unit in (APP / 'systemd').iterdir():
        shutil.copy2(unit, Path('/etc/systemd/system') / unit.name)
    dropin = Path('/etc/systemd/system/docker.service.d')
    dropin.mkdir(exist_ok=True)
    (dropin / 'adblock.conf').write_text('[Unit]\nRequires=adblock-firewall.service\nAfter=adblock-firewall.service\n')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'adblock-network-rollback.timer', capture=False)
    run('systemctl', 'enable', '--now', '--no-block', 'adblock.service', capture=False)


def backup(protect=None):
    if PENDING.exists():
        raise ValueError('Confirm or roll back pending network changes before backup')
    folder = DATA / 'backups'
    folder.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = folder / (stamp + '.tar.gz')
    compose('stop', capture=False)
    try:
        with tarfile.open(dest, 'w:gz') as archive:
            archive.add(ETC, arcname='etc')
            for name in (*SERVICES, 'profiles'):
                archive.add(DATA / name, arcname='data/' + name)
        dest.chmod(0o600)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        start()
    protected = {Path(protect).resolve()} if protect else set()
    marker = DATA / 'rollback.json'
    if marker.exists():
        protected.add(Path(json.loads(marker.read_text())['backup']).resolve())
    for old in sorted(folder.glob('*.tar.gz'))[:-5]:
        if old.resolve() not in protected:
            old.unlink()
    print(dest)
    return dest


def validate_archive(archive):
    for member in archive.getmembers():
        parts = Path(member.name).parts
        if not parts or member.name.startswith('/') or '..' in parts or '\\' in member.name:
            raise ValueError('Unsafe archive path')
        if parts[0] not in ('etc', 'data') or not (member.isfile() or member.isdir()):
            raise ValueError('Archive contains unexpected paths, links or devices')
        if parts[0] == 'data' and len(parts) > 1 and parts[1] not in (*SERVICES, 'profiles'):
            raise ValueError('Unexpected data directory')


def restore(path):
    if PENDING.exists():
        raise ValueError('Pending network transaction; restore is unavailable')
    with tempfile.TemporaryDirectory(dir=DATA) as temp:
        root = Path(temp)
        with tarfile.open(path, 'r:gz') as archive:
            validate_archive(archive)
            archive.extractall(root, filter='data')
        json.loads((root / 'etc/config.json').read_text())
        current = config()
        # Restoring DNS data must not silently change the machine's actual LAN identity.
        (root / 'etc/config.json').write_text(json.dumps(current))
        for required in ('etc/images.env', 'etc/unbound.conf', 'etc/webpassword', 'data/pihole', 'data/unbound', 'data/tailscale', 'data/profiles'):
            if not (root / required).exists():
                raise ValueError('Incomplete backup: ' + required)
        compose('down', capture=False)
        saved = root / 'previous'
        saved.mkdir()
        pairs = [(ETC, root / 'etc')] + [(DATA / n, root / 'data' / n) for n in (*SERVICES, 'profiles')]
        moved = []
        try:
            for index, (target, incoming) in enumerate(pairs):
                previous = saved / str(index)
                shutil.move(str(target), previous)
                moved.append((target, previous))
                shutil.move(str(incoming), target)
            compose('config', '--quiet')
            start()
            wait_dns()
        except BaseException:
            compose('down', check=False)
            for target, previous in reversed(moved):
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(previous), target)
            start()
            raise
        print('Restored data and image pins. Current LAN configuration preserved.')


def wait_dns():
    for _ in range(12):
        if dns_ok():
            return
        time.sleep(2)
    raise ValueError('DNS health check failed')


def network_rollback(force=False):
    if not PENDING.exists():
        return
    pending = json.loads(PENDING.read_text())
    if pending.get('confirmed'):
        run('nmcli', 'connection', 'delete', 'uuid', pending['backup_uuid'], check=False)
        PENDING.unlink()
        return
    if not force and time.time() < pending['deadline']:
        return
    settings = pending['old_settings']
    args = [item for pair in settings.items() for item in pair]
    run('nmcli', 'connection', 'modify', pending['new_uuid'], *args)
    run('nmcli', 'connection', 'up', 'uuid', pending['new_uuid'], capture=False)
    restored = dict(pending['old'])
    actual = detect_network()
    if actual['interface'] == restored['interface']:
        restored['address'] = actual['address']
        restored['gateway'] = actual['gateway']
    atomic_json(CONFIG, restored)
    firewall(restored)
    run('nmcli', 'connection', 'delete', 'uuid', pending['backup_uuid'], check=False)
    PENDING.unlink()
    print('Previous LAN configuration restored.')


def network_set(args):
    if PENDING.exists():
        raise ValueError('A network change is already pending')
    old = config()
    if args.profile:
        new = json.loads((DATA / 'profiles' / (safe_name(args.profile) + '.json')).read_text())
        new = valid_network(new['interface'], new['address'], new['gateway'], new['allow_cidrs'])
    else:
        if not all((args.interface, args.address, args.gateway, args.allow_cidr)):
            raise ValueError('Provide --interface, --address, --gateway and --allow-cidr')
        new = valid_network(args.interface, args.address, args.gateway, args.allow_cidr)
    if new['interface'] != old['interface']:
        raise ValueError('Changing managed interface is not supported; use the current LAN interface')
    candidates = args.host_dns or re.split(r'[\s|,;]+', run('nmcli', '-g', 'IP4.DNS', 'device', 'show', new['interface']).stdout.strip())
    host_dns = []
    for candidate in candidates:
        if not candidate:
            continue
        dns = ipaddress.IPv4Address(candidate.strip())
        if not dns.is_loopback and str(dns) not in (str(ipaddress.ip_interface(old['address']).ip), str(ipaddress.ip_interface(new['address']).ip)) and dns not in ipaddress.ip_network('100.64.0.0/10'):
            host_dns.append(str(dns))
    if not host_dns:
        raise ValueError('Provide --host-dns with a reachable external resolver to avoid a DNS bootstrap loop')
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all').stdout)
    subnet = ipaddress.ip_interface(new['address']).network
    for route in routes:
        dst = route.get('dst', 'default')
        if dst != 'default' and route.get('dev') not in (new['interface'], 'lo'):
            if subnet.overlaps(ipaddress.ip_network(dst, strict=False)):
                raise ValueError('New subnet overlaps another interface route: ' + dst)
    new_ip = str(ipaddress.ip_interface(new['address']).ip)
    if new_ip != str(ipaddress.ip_interface(old['address']).ip):
        run('arping', '-D', '-c', '3', '-w', '5', '-I', new['interface'], new_ip)
    uuid = run('nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', new['interface']).stdout.strip()
    fields = ('ipv4.method', 'ipv4.addresses', 'ipv4.gateway', 'ipv4.dns', 'ipv4.ignore-auto-dns')
    old_settings = {field: run('nmcli', '-g', field, 'connection', 'show', uuid).stdout.strip() for field in fields}
    for field in ('ipv4.addresses', 'ipv4.dns'):
        old_settings[field] = ','.join(filter(None, re.split(r'[\s|,;]+', old_settings[field])))
    old_name = run('nmcli', '-g', 'connection.id', 'connection', 'show', uuid).stdout.strip()
    clone = 'adblock-recovery-' + str(time.time_ns())
    run('nmcli', 'connection', 'clone', uuid, clone)
    backup_uuid = run('nmcli', '-g', 'connection.uuid', 'connection', 'show', clone).stdout.strip()
    run('nmcli', 'connection', 'modify', backup_uuid, 'connection.autoconnect', 'no')
    original_auto = run('nmcli', '-g', 'connection.autoconnect', 'connection', 'show', uuid).stdout.strip()
    pending = dict(old=old, new=new, backup_uuid=backup_uuid, new_uuid=uuid,
                   old_name=old_name, original_auto=original_auto, old_settings=old_settings, host_dns=host_dns, deadline=time.time() + 120)
    atomic_json(PENDING, pending)
    # The system service executes this after the SSH client returns, independently of its lifetime.
    run('systemd-run', '--unit=adblock-network-apply', '--collect', '--on-active=2s',
        '/usr/local/bin/adblock', '_network-apply', capture=False)
    print(f'Reconnect to {new_ip} and run sudo adblock network confirm within 120 seconds.')


def network_apply():
    if not PENDING.exists():
        return
    p = json.loads(PENDING.read_text())
    n = p['new']
    try:
        run('nmcli', 'connection', 'modify', p['new_uuid'], 'ipv4.method', 'manual',
            'ipv4.addresses', n['address'], 'ipv4.gateway', n['gateway'],
            'ipv4.dns', ','.join(p['host_dns']), 'ipv4.ignore-auto-dns', 'yes')
        run('nmcli', 'connection', 'up', 'uuid', p['new_uuid'])
        atomic_json(CONFIG, n)
        firewall(n)
        wait_dns()
        p['applied'] = True
        atomic_json(PENDING, p)
    except BaseException:
        network_rollback(force=True)
        raise


def safe_name(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('Profile names must contain only letters, digits, underscore or hyphen')
    return value


def network_confirm():
    if not PENDING.exists():
        raise ValueError('No pending network change')
    p = json.loads(PENDING.read_text())
    if time.time() >= p['deadline']:
        network_rollback(force=True)
        raise ValueError('Confirmation deadline expired')
    if not p.get('applied') or config() != p['new'] or not dns_ok():
        raise ValueError('New configuration is not healthy yet')
    p['confirmed'] = True
    atomic_json(PENDING, p)
    run('nmcli', 'connection', 'delete', 'uuid', p['backup_uuid'])
    PENDING.unlink()
    print('Network configuration confirmed.')


def diagnostics():
    checks = {}
    for name, command in {
        'docker': ('docker', 'info'),
        'network_manager': ('systemctl', 'is-active', 'NetworkManager'),
        'firewall': ('nft', 'list', 'table', 'inet', 'adblock'),
        'time_synced': ('timedatectl', 'show', '-p', 'NTPSynchronized', '--value'),
    }.items():
        try:
            r = run(*command, check=False)
            checks[name] = r.returncode == 0 and (name != 'time_synced' or r.stdout.strip() == 'yes')
        except OSError:
            checks[name] = False
    checks['dns'] = dns_ok()
    valid = run('dig', '@127.0.0.1', '-p', '5335', 'cloudflare.com', '+dnssec', '+time=3', '+tries=1', check=False).stdout
    bogus = run('dig', '@127.0.0.1', '-p', '5335', 'dnssec-failed.org', '+time=3', '+tries=1', check=False).stdout
    checks['dnssec_valid'] = 'status: NOERROR' in valid and bool(re.search(r'flags: [^;]*\bad\b', valid))
    checks['dnssec_bogus_rejected'] = 'status: SERVFAIL' in bogus
    checks['disk_space'] = shutil.disk_usage(DATA).free >= 1024**3
    ts = compose('exec', '-T', 'tailscale', 'tailscale', 'status', '--json', check=False)
    try:
        checks['tailscale_authenticated'] = json.loads(ts.stdout).get('BackendState') == 'Running'
    except ValueError:
        checks['tailscale_authenticated'] = False
    return checks


def parser():
    p = argparse.ArgumentParser(prog='adblock')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('install', 'update'):
        s = sub.add_parser(name)
        s.add_argument('--pihole-image')
        s.add_argument('--tailscale-image')
        s.add_argument('--base-image', default='debian:trixie-slim')
    for name in ('start', 'stop', 'restart', 'backup', 'rollback', '_network-rollback', '_network-apply', '_firewall'):
        sub.add_parser(name)
    for name in ('status', 'doctor'):
        sub.add_parser(name).add_argument('--json', action='store_true')
    sub.add_parser('restore').add_argument('file', type=Path)
    s = sub.add_parser('logs')
    s.add_argument('service', nargs='?', choices=SERVICES)
    sub.add_parser('uninstall').add_argument('--purge-data', action='store_true')
    s = sub.add_parser('tailscale')
    s.add_argument('action', choices=('login', 'status'))
    for name in ('block', 'allow'):
        s = sub.add_parser(name)
        s.add_argument('action', choices=('add', 'remove'))
        s.add_argument('domain')
    s = sub.add_parser('lists')
    ls = s.add_subparsers(dest='action')
    ls.add_parser('list').add_argument('--json', action='store_true')
    sub.add_parser('gravity').add_subparsers(dest='action', required=True).add_parser('update')
    n = sub.add_parser('network').add_subparsers(dest='action', required=True)
    n.add_parser('show')
    n.add_parser('confirm')
    s = n.add_parser('set')
    for flag in ('interface', 'address', 'gateway', 'profile'):
        s.add_argument('--' + flag)
    s.add_argument('--allow-cidr', action='append')
    s.add_argument('--host-dns', action='append', help='Host bootstrap resolver; defaults to current LAN DNS')
    n.add_parser('save').add_argument('name')
    return p


def execute(a):
    cmd = a.command
    if cmd == 'lists':
        return manage_lists(a)
    if cmd == 'gravity':
        return gravity_update()
    if PENDING.exists() and cmd not in ('network', 'status', '_network-apply', '_network-rollback', '_firewall', 'start'):
        raise ValueError('Network change pending; confirm it or wait for automatic recovery first')
    if cmd == 'install':
        install(a)
    elif cmd == 'start':
        start()
    elif cmd == '_firewall':
        firewall(config())
    elif cmd == 'stop':
        compose('stop', capture=False)
    elif cmd == 'restart':
        firewall(config())
        compose('restart', capture=False)
    elif cmd == 'status':
        r = compose('ps', '--all', '--format', 'json', check=False)
        print(r.stdout if a.json else 'LAN: ' + config()['address'] + '\n' + r.stdout)
        return r.returncode
    elif cmd == 'doctor':
        result = diagnostics()
        print(json.dumps(result, indent=2) if a.json else '\n'.join(f"{k}: {'OK' if v else 'FAIL'}" for k, v in result.items()))
        return 0 if all(result.values()) else 1
    elif cmd == 'logs':
        compose('logs', '--tail', '100', *( [a.service] if a.service else []), capture=False)
    elif cmd == 'tailscale':
        command = ('login', '--accept-dns=false') if a.action == 'login' else ('status',)
        compose('exec', '-T', 'tailscale', 'tailscale', *command, capture=False)
        if a.action == 'login':
            compose('exec', '-T', 'tailscale', 'tailscale', 'ip', '-4', capture=False)
            print('Register this IP as the Tailnet global DNS server and enable DNS override; permit DNS and SSH in Tailnet grants.')
    elif cmd in ('block', 'allow'):
        domain = a.domain.encode('idna').decode('ascii').lower().rstrip('.')
        if len(domain) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in domain.split('.')):
            raise ValueError('Invalid domain name')
        action = 'allow' if cmd == 'allow' else 'deny'
        compose('exec', '-T', 'pihole', 'pihole', action, *(['remove'] if a.action == 'remove' else []), domain, capture=False)
    elif cmd == 'network':
        if a.action == 'show':
            print(json.dumps(config(), indent=2))
            compose('exec', '-T', 'tailscale', 'tailscale', 'ip', '-4', capture=False)
        elif a.action == 'set':
            network_set(a)
        elif a.action == 'confirm':
            network_confirm()
        elif a.action == 'save':
            atomic_json(DATA / 'profiles' / (safe_name(a.name) + '.json'), config())
    elif cmd == '_network-apply':
        network_apply()
    elif cmd == '_network-rollback':
        network_rollback()
    elif cmd == 'backup':
        backup()
    elif cmd == 'restore':
        target = a.file.resolve()
        with tarfile.open(target, 'r:gz') as archive:
            validate_archive(archive)
        backup(protect=target)
        restore(target)
    elif cmd == 'update':
        if not a.pihole_image or not a.tailscale_image:
            raise ValueError('Update requires explicit reviewed --pihole-image and --tailscale-image')
        images = prepare_images(a.pihole_image, a.tailscale_image, a.base_image)
        previous = backup()
        atomic_json(DATA / 'rollback.json', {'backup': str(previous)})
        try:
            write_images(images)
            compose('config', '--quiet')
            start()
            wait_dns()
        except BaseException:
            restore(previous)
            raise
    elif cmd == 'rollback':
        restore(Path(json.loads((DATA / 'rollback.json').read_text())['backup']))
    elif cmd == 'uninstall':
        if PENDING.exists():
            network_rollback(force=True)
        run('systemctl', 'disable', '--now', 'adblock.service', 'adblock-network-rollback.timer', capture=False)
        compose('down', capture=False)
        run('nft', 'delete', 'table', 'inet', 'adblock', check=False)
        Path('/etc/systemd/system/docker.service.d/adblock.conf').unlink(missing_ok=True)
        for unit in ('adblock.service', 'adblock-firewall.service', 'adblock-network-rollback.service', 'adblock-network-rollback.timer'):
            (Path('/etc/systemd/system') / unit).unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')
        if a.purge_data:
            shutil.rmtree(DATA)
            shutil.rmtree(ETC)
        print('Services removed. LAN configuration retained. Data ' + ('deleted.' if a.purge_data else 'retained.'))
    return 0


def main():
    # Keep generated help, errors and child-process messages in English.
    os.environ.update(LC_ALL='C', LANGUAGE='C')
    a = parser().parse_args()
    if platform.system() != 'Linux':
        raise ValueError('Execution requires Linux; --help works on other platforms')
    if os.geteuid() != 0:
        raise ValueError('Docker and network inspection require root: run with sudo')
    # Docker may socket-activate while another operation holds the command lock.
    # This boot dependency only installs an atomic ruleset and must not wait on it.
    if a.command == '_firewall':
        firewall(config())
        return 0
    # Do not hold the recovery lock while waiting for interactive input.
    if a.command == 'lists':
        return manage_lists(a)
    with command_lock():
        return execute(a)


@contextlib.contextmanager
def command_lock():
    import fcntl
    with Path('/run/lock/adblock.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield



if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('adblock: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)
