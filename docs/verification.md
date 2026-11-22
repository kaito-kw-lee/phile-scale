# Verification Guide

[← Back to README](../README.md)

[Development checks](#development-checks) · [Recorded results](#recorded-hardware-results) · [Unverified scenarios](#not-yet-verified-on-hardware) · [Acceptance checklist](#hardware-acceptance-checklist)

## Development Checks

Run from the repository root:

```sh
python -m unittest discover -s tests -v
python -m py_compile adblock.py
```

The unit suites in `tests/test_adblock.py` and `tests/test_lists.py` cover address validation, firewall replacement, network recovery, startup readiness, backups, and interactive list selection/API/save/Gravity behavior. Unit checks do not establish hardware compatibility.

The interactive selector was tested on `tzspec-pi` on **2026-11-22**, using staged
source against the running Pi-hole (Core v6.6, Web v6.6.1, FTL v6.9). The installed CLI
was not replaced. See [`verification/lists-raspberry-pi.json`](../verification/lists-raspberry-pi.json).
All 24 unit tests passed on the Pi. Real pseudo-terminal tests covered Esc cancellation,
arrow/Space selection, Enter save and automatic Gravity, DNS blocking/unblocking,
metadata preservation, and disabling without deletion. Original subscriptions were
restored and all nine diagnostic checks passed before and after testing.

`tests/live_lists.py` reproduces this explicit acceptance test from a staged repository
as root. It temporarily subscribes to a loopback HTTP fixture blocking `example.net`,
runs Gravity, and removes the subscription in cleanup. It requires that `example.net`
resolves normally before the test. It uses the installed Compose configuration and
the staged recommendation catalog. Run only when temporary blocking of that domain
and Gravity refreshes are acceptable. No service stop or CLI installation is required.

Actual terminal resizing, Ctrl+C during download, API outage and partial-save failure
remain unverified on hardware; relevant unit coverage does not replace those checks.

## Recorded Hardware Results

The source of truth is [`verification/raspberry-pi.json`](../verification/raspberry-pi.json), recorded on **2026-11-15**. These are historical results, not checks performed each time this document is read or updated.

| Environment | Recorded value |
| --- | --- |
| Hardware | Raspberry Pi 4 |
| OS | Debian 13 (trixie), aarch64 |
| Pi-hole image | 2026.11.0 |
| Tailscale | 1.104.7 |
| Unbound | 1.26.2-0+deb13u1 |
| Docker / Compose | 26.1.5 / 2.26.1 |
| Unit tests / diagnostic checks | 14 passed / 9 passed |

The tested official image digests are pinned in [`release-images.env`](../release-images.env).

Recorded passing scenarios:

- ARM64 installation and Compose startup.
- UDP and TCP DNS queries from the Pi and a separate LAN PC, plus Unbound recursive resolution.
- DNSSEC validation: AD flag for a valid response and SERVFAIL for an invalid signature.
- Pi-hole deny rules, allow overrides, and list-entry deletion.
- Backup with services stopped and restoration of the previous lists.
- Denied network namespace access to TCP/UDP ports 53 and 5335.
- Automatic recovery of an unconfirmed network change after 120 seconds, preserving the original NetworkManager UUID and DHCP settings; SSH reconnection and actual-address refresh after recovery.
- Reboot startup with the firewall before Docker and the adblock service.
- Tailscale login and address assignment, with authentication and address preserved across container restart.
- DNS queries to the Tailscale address from the Pi and a user-confirmed query from a separate Windows Tailscale client returning A and AAAA answers.
- All nine `adblock doctor` checks passing.

## Not Yet Verified on Hardware

The recorded evidence explicitly leaves these scenarios unverified:

- Tailnet global DNS override configuration.
- Router DHCP DNS settings and address reservation.
- Cross-subnet IP migration and physical power loss during a network change.
- Real image-upgrade failure and automatic data rollback.
- Disk-exhaustion and authentication-expiry fault injection.

The checklist below defines acceptance work; inclusion in it does not imply a recorded pass. Consult the evidence before claiming coverage for additional scenarios, such as IPv6 denial or reboot during a pending network change.

## Hardware Acceptance Checklist

Run on the installed Pi and appropriate client devices:

1. After installation, check `adblock doctor` results for DNS, DNSSEC, firewall, time, disk, and Tailscale.
2. From both LAN and external Tailscale clients, run `dig @PI_IP example.com` and `dig +tcp @PI_IP example.com`, replacing `PI_IP` with the address for each path.
3. Temporarily block `example.com`, verify blocked responses through both paths, then remove the rule.
4. Verify DNS access fails from a disallowed LAN client and port 5335 is inaccessible externally. IPv6 port 53 should also be inaccessible.
5. Exercise successful network confirmation, unconfirmed timeout, SSH disconnection, and reboot during a pending change separately.
6. After reboot, verify that the firewall starts before Docker and that DNS service and Tailscale authentication recover.
7. Create a backup, change a block-list entry, and restore to verify the original state. Also test update failure and rollback.
8. Check diagnostics for Unbound failure, port conflicts, low disk space, and expired Tailscale authentication.

[`tests/live_verify.py`](../tests/live_verify.py) automates a subset of these checks: diagnostics, DNS, lists, backup/restore, and denied ingress. It is an explicit live test, not a read-only health check. Run it as root on the installed Pi over LAN SSH from the repository root:

```sh
sudo python3 tests/live_verify.py
```

The script loads the installed application from `/opt/adblock/current/adblock.py`, temporarily changes `example.net` list entries, stops the stack for backup/restore, and creates a private test network namespace. Do not run it against an existing customized `example.net` policy. Complete the remaining client, router, Tailnet, and failure scenarios separately.
