#!/bin/sh
set -eu
umask 077
mkdir -p /run/sshd /var/lib/cursor-login
chown cursor:cursor /var/lib/cursor-login
chmod 700 /var/lib/cursor-login
exec /usr/sbin/sshd -D -e -f /etc/ssh/cursor_sshd_config
