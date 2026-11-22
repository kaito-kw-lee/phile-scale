# Operations Guide

[← Back to README](../README.md)

[Installation](#installation) · [LAN DNS](#lan-dns) · [Remote access](#tailscale-and-remote-access) · [Network changes](#changing-the-lan-address) · [Commands](#everyday-commands) · [Storage](#storage-and-web-ui) · [Maintenance](#backups-updates-and-rollback) · [Removal](#removal) · [References](#references)

## Installation

Use an ARM64 host with Debian 13 or Debian 13-based Raspberry Pi OS, systemd, and NetworkManager. 32-bit systems are unsupported. Newer OS versions must pass preflight checks; unreleased versions are not guaranteed to work.

Copy the repository to the Pi and run these commands from its root directory:

```sh
# Recorded hardware versions: Pi-hole 2026.11.0 / Tailscale v1.104.7
. ./release-images.env
sudo sh install.sh \
  --pihole-image "$PIHOLE_IMAGE" \
  --tailscale-image "$TAILSCALE_IMAGE" \
  --base-image "$BASE_IMAGE"
sudo systemctl status adblock
sudo adblock tailscale login
sudo adblock doctor
```

Supply reviewed official Pi-hole v6 and Tailscale image tags or digests. The installer does not default to `latest`; it resolves tags to immutable digests. Unbound is built from a Debian base image pinned by digest and then pinned by its local image ID. The repository's [`release-images.env`](../release-images.env) contains the image digests used in the recorded verification.

The installer uses Debian's `docker.io` and `docker-compose` packages when needed and preserves an existing Docker installation. Preflight checks cover NTP synchronization, at least 2 GiB of free disk space, `/dev/net/tun`, and port 53 availability. Existing DNS services are not stopped automatically. Image downloads require internet access, and Unbound recursion requires outbound TCP/UDP 53.

The installation completion message confirms registration of the systemd start request. Check `systemctl status adblock` and `adblock doctor` for the actual runtime result. Tailscale authentication diagnostics are expected to fail before login.

## LAN DNS

LAN DNS configuration is optional. For personal devices using [Tailscale DNS](#tailscale-and-remote-access) at home or away, leave the router's DNS and DHCP settings unchanged; the Pi still connects to the LAN normally.

For LAN-wide filtering, the initial LAN IP is detected from the active connection. Reserve that address in your router's DHCP settings, then set the LAN DHCP DNS server to the Pi's IP and renew client leases. This is the DNS address advertised to clients, distinct from the router's internet/WAN DNS setting. Router configuration is a manual step. For selected LAN devices only, keep the Pi's LAN address stable and set DNS on those devices manually instead.

Sharing a LAN does not automatically enable filtering: clients must use Pi-hole for DNS. The package allows DNS from configured LAN ranges and Tailscale, so choosing personal Tailscale use does not block direct LAN access to Pi-hole.

Router-advertised IPv6 DNS and browser or app DNS-over-HTTPS may bypass Pi-hole; configure them separately. This package blocks inbound IPv6 DNS and does not provide DHCP service.

Existing host firewalls may need separate DNS allow rules. The package's firewall cannot override another firewall's DROP rules.

## Tailscale and Remote Access

Run `sudo adblock tailscale login` and open the URL it prints. Register the displayed Tailscale IPv4 address as a global nameserver in your Tailnet and enable DNS override. Clients must accept Tailscale DNS. Tailnet grants or ACLs must allow the intended devices to reach TCP/UDP 53 and administrators to reach SSH port 22. The Pi itself uses `--accept-dns=false`.

```sh
sudo adblock tailscale status
# Replace PI_USER and PI_TAILSCALE_IP with your SSH user and the Pi's address.
ssh PI_USER@PI_TAILSCALE_IP
```

The package does not configure an exit node or subnet routing. It uses your existing SSH server without changing SSH installation or authentication policy.

The container runs `tailscaled` directly. The daemon stays running before authentication, so waiting for login does not cause container restarts due to a login timeout. Login is performed explicitly through the CLI.

## Changing the LAN Address

Check the actual interface with `network show` before adapting this example:

```sh
sudo adblock network show
sudo adblock network save home
sudo adblock network set --interface eth0 --address 192.168.10.2/24 \
  --gateway 192.168.10.1 --allow-cidr 192.168.10.0/24

# Reconnect through the new IP or Tailscale and confirm within 120 seconds.
sudo adblock network confirm

# Reuse a saved configuration; reconnect and confirm this change too.
sudo adblock network set --profile home
sudo adblock network confirm
```

Repeat `--allow-cidr` to allow multiple networks. The host's bootstrap resolver preserves the current LAN DNS by default; repeat `--host-dns DNS_IP` when explicit resolvers are needed. When moving between subnets, specify host DNS reachable from the new network and ensure the router and physical connection match that subnet. Duplicate-IP detection only detects responding devices on the same link. Changing the interface itself is unsupported.

Before applying a change through a systemd job, the CLI journals the original NetworkManager profile's IPv4 settings and keeps a cloned profile. Recovery restores the original UUID's settings, preserving DHCP connection identity.

A persistent journal and an independent five-second recovery timer replace an interactive NetworkManager checkpoint. Recovery is retried after SSH disconnects and reboots. Unconfirmed changes are restored on the next timer tick after 120 seconds. If DHCP assigns a different address, the actual address is reflected in configuration. Long-running operations such as backups and updates are blocked while a network change is pending.

## Everyday Commands

```sh
sudo adblock status --json
sudo adblock doctor --json
sudo adblock logs pihole
sudo adblock block add ads.example.com
sudo adblock block remove ads.example.com
sudo adblock allow add trusted.example.com
sudo adblock allow remove trusted.example.com
sudo adblock stop
sudo adblock start
sudo adblock restart
```

`status` and `doctor` also work without `--json`. Logs can target `pihole`, `unbound`, or `tailscale`; omit the service to view stack logs. All commands, including queries, require `sudo` because they access the Docker socket and private configuration.

## Storage and Web UI

| Location | Purpose |
| --- | --- |
| `/etc/adblock` | Configuration |
| `/var/lib/adblock` | Persistent data |
| `/opt/adblock/current` | Runtime assets |
| `/etc/adblock/webpassword` | Generated web UI password |

The web UI listens only on `127.0.0.1:8080` on the Pi. Basic operation does not require it. To access it from another machine using the existing SSH server:

```sh
# Replace PI_USER and PI_IP before running on your client machine.
ssh -L 8080:127.0.0.1:8080 PI_USER@PI_IP
```

With the tunnel open, visit `http://127.0.0.1:8080/admin/` on that client. Read the generated password on the Pi with `sudo cat /etc/adblock/webpassword`.

Pi-hole query history is retained for seven days. Container logs are limited to three 10 MB files per service.

## Backups, Updates, and Rollback

Backups stop services to keep databases consistent, causing a brief DNS outage. Backups, restores, and updates also stop the Tailscale container. Run maintenance through LAN SSH or a local console. For remote maintenance, use an independent systemd job so it survives disconnection:

```sh
sudo systemd-run --unit=adblock-maintenance --collect /usr/local/bin/adblock backup
# After reconnecting:
sudo journalctl -u adblock-maintenance
```

For local or LAN maintenance, choose the command for the operation you need:

| Operation | Command |
| --- | --- |
| Back up | `sudo adblock backup` |
| Restore | `sudo adblock restore /var/lib/adblock/backups/BACKUP_FILE.tar.gz` |
| Update | `sudo adblock update --pihole-image "$PIHOLE_IMAGE" --tailscale-image "$TAILSCALE_IMAGE"` |
| Roll back | `sudo adblock rollback` |

Replace `BACKUP_FILE.tar.gz` with an existing backup filename. Before updating, set `PIHOLE_IMAGE` and `TAILSCALE_IMAGE` to the reviewed official tags or digests you intend to deploy. Loading `. ./release-images.env` from the repository root selects the recorded hardware versions. Updates also accept `--base-image` to select the Unbound build base.

The five most recent backups are retained, with additional backups possible when protected as rollback or current restore targets. Backups include Tailscale authentication data; keep off-host copies private too.

On update failure, the implementation restores both image pins and the data backup. Restores preserve current LAN settings. Real image-upgrade failure and automatic data rollback remain [unverified on hardware](verification.md#not-yet-verified-on-hardware).

When moving backups off-host, separately preserve the local Unbound image with `docker image save`. Avoid automatic image pruning during the rollback retention period because it may delete the previous Unbound image. Pinning the base image digest does not make rebuilds bit-for-bit reproducible: apt packages still come from the repositories available at build time.

## Removal

```sh
sudo adblock uninstall
# Only when you also intend to delete data:
sudo adblock uninstall --purge-data
```

Removal deletes services and package-owned firewall rules. Docker, other containers, host LAN settings, and source files are preserved. After uninstalling while retaining data, running the installer again re-registers the systemd units.

## Interactive Block-list Selection

To deploy this CLI feature to an existing Pi, copy the updated repository (including
`recommended-lists.json`) and run `sudo sh install.sh` from its root. The existing-install
path refreshes application files while retaining configuration and pinned images.
`adblock update` updates container images; it does not fetch new CLI source files.

Run `sudo adblock lists` in an interactive terminal (allocate a TTY when using SSH).
The selector appears directly below the command in the normal terminal buffer;
previous output and scrollback remain available. Only the selector's lines are redrawn.
After a resize, a new selector block may be appended to avoid overwriting reflowed output.
ANSI support is required; `TERM=dumb` is rejected with a read-only alternative.
Use Up/Down to move, Space to toggle, and Enter to save and update Gravity.
Esc or Ctrl+C in the selection screen cancels without saving. The selected row's
URL appears at the bottom; the screen scrolls to follow the cursor.

Recommendations are a reviewed subset of [Firebog's ticked lists](https://v.firebog.net/hosts/lists.php?type=tick),
bundled in `recommended-lists.json`: AdAway, EasyList and EasyPrivacy. The catalog
records its review date and changes with project releases, not at runtime.
The contents of enabled lists are downloaded afresh by Gravity. New recommendations
start unchecked; existing entries reflect Pi-hole's current enabled state.

Each recommendation displays `Recommended by: firebog.net`. Other registered lists
display `Download host:` instead; no recommendation source is inferred for custom
lists. The selected item's actual URL appears separately as `Download:`. The catalog's
top-level `source` is the recommendation default, with optional per-entry `source`
overrides; `snapshot_url` records where the reviewed recommendation snapshot came from.

All CLI-generated labels, help, status and error messages are English. Child processes
receive `LC_ALL=C` and `LANGUAGE=C`; Gravity also receives these inside its container.
Original list names, URLs and user-provided descriptions are not translated. Control
characters are sanitized in the selector, and long lines are clipped to terminal width.

Existing custom block lists also appear. Unchecking disables rather than deletes
an entry. New checked entries join Pi-hole's default group; existing group assignments
and comments are retained. Group membership still determines which clients use a
list. Existing allow-list subscriptions and individual domain rules are not modified.
Use `sudo adblock allow add DOMAIN` for individual domain exceptions.

Enter closes the selector, saves changes through the local Pi-hole v6 API, and runs
`pihole -g` with visible progress. Enter also refreshes Gravity when the selection is
unchanged. Saving enabled flags can affect Pi-hole before the download finishes;
save and refresh are not an atomic transaction.

If another administrator changes the list configuration while you select, saving
stops and asks you to reopen the selector. This is an optimistic check, not an API
transaction locking out concurrent web UI edits. Partial save failures report saved,
uncertain and unattempted entries and skip Gravity; reopen the selector to review.
If Gravity fails or is interrupted, the saved selection remains. Retry with:

```sh
sudo adblock gravity update
```

Review the output for individual download warnings even when the command exits
successfully. For scripts or noninteractive sessions, inspect registered block lists:

```sh
sudo adblock lists list
sudo adblock lists list --json
```

API requests use the existing `/etc/adblock/webpassword` and the loopback web service.
If you changed the web password independently or enabled 2FA, authentication may fail;
this selector does not prompt for a second factor. It does not modify authentication settings.

## References

- [Pi-hole Docker](https://docs.pi-hole.net/docker/)
- [Pi-hole CLI](https://docs.pi-hole.net/main/pihole-command/)
- [Unbound with Pi-hole](https://docs.pi-hole.net/guides/dns/unbound/)
- [Tailscale Docker](https://tailscale.com/docs/features/containers/docker)
- [Debian Compose package](https://packages.debian.org/trixie/docker-compose)
