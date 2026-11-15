#!/bin/sh
set -eu
if [ "$(id -u)" != 0 ]; then echo 'Run with sudo' >&2; exit 1; fi
cd "$(dirname "$0")"
exec python3 ./adblock.py install "$@"
