#!/usr/bin/env python3
"""Raspberry Pi DNS appliance. All subprocess arguments are passed without a shell."""
import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ETC = Path('/etc/adblock')
DATA = Path('/var/lib/adblock')
APP = Path('/opt/adblock/current')
CONFIG = ETC / 'config.json'
PENDING = DATA / 'network-pending.json'
SERVICES = ('unbound', 'pihole', 'tailscale')


def run(*args, check=True, capture=True, input=None):
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          capture_output=capture, input=input)


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
    for name in ('compose.yaml', 'adblock.py', 'unbound', 'systemd'):
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
        for name in ('compose.yaml', 'adblock.py', 'unbound', 'systemd'):
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
    # Serialize every operation with rollback jobs, including confirmation at the deadline.
    import fcntl
    lock_path = Path('/run/lock/adblock.lock')
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return execute(a)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('adblock: ' + str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(1)
