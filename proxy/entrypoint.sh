#!/bin/sh
set -eu
python3 - <<'PY'
import os
import re
from pathlib import Path

password = os.environ.get('SQUID_MANAGER_PASSWORD', '')
if not re.fullmatch(r'[A-Za-z0-9_-]{24,128}', password):
    raise SystemExit('SQUID_MANAGER_PASSWORD must contain 24-128 letters, digits, underscores or hyphens')
template = Path('/etc/squid-deb-proxy/ui-squid.conf.template').read_text()
target = Path('/etc/squid-deb-proxy/ui-squid.conf')
target.write_text(template.replace('__MANAGER_PASSWORD__', password))
target.chmod(0o640)
PY
mkdir -p /var/lib/squid-mirrors
if [ ! -f /var/lib/squid-mirrors/mirrors.acl ]; then
    cp /etc/squid-deb-proxy/ui-mirrors.acl /var/lib/squid-mirrors/mirrors.acl
fi
chown -R 10001:10001 /var/lib/squid-mirrors
chmod 755 /var/lib/squid-mirrors
chmod 644 /var/lib/squid-mirrors/mirrors.acl
mkdir -p /var/cache/squid-deb-proxy /var/log/squid-deb-proxy /run/squid-deb-proxy
chown -R proxy:proxy /var/cache/squid-deb-proxy /var/log/squid-deb-proxy /run/squid-deb-proxy
chown root:proxy /etc/squid-deb-proxy/ui-squid.conf
chmod 755 /var/log/squid-deb-proxy
touch /var/log/squid-deb-proxy/access.log
chown proxy:proxy /var/log/squid-deb-proxy/access.log
chmod 644 /var/log/squid-deb-proxy/access.log
if ! squid -f /etc/squid-deb-proxy/ui-squid.conf -k parse >/dev/null 2>&1; then
    echo 'Squid configuration validation failed; check the configuration template and mirror allowlist.' >&2
    exit 1
fi
# Foreground initialization waits for every directory and repairs interrupted setup.
squid -N -f /etc/squid-deb-proxy/ui-squid.conf -z
exec squid -N -f /etc/squid-deb-proxy/ui-squid.conf
