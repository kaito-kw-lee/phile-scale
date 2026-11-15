#!/bin/sh
set -eu
mkdir -p /var/lib/unbound
if [ ! -s /var/lib/unbound/root.key ]; then
    cp /usr/share/dns/root.key /var/lib/unbound/root.key
fi
chown -R unbound:unbound /var/lib/unbound
exec unbound -d -c /etc/unbound/unbound.conf
