#!/usr/bin/python3
"""Operator-only login/status/models helper; not reachable via SSH."""

import fcntl
import os
import sys
import tempfile

sys.path.insert(0, "/opt/cursor-runner")
from cursor_plugin.runner import AGENT, LOCK, cli_env

if len(sys.argv) != 2 or sys.argv[1] not in {"login", "logout", "status", "models"}:
    raise SystemExit("Usage: cursor-admin login|logout|status|models")
os.umask(0o077)
lock = open(LOCK, "a+b")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("Cursor is busy; try again later.") from None
os.set_inheritable(lock.fileno(), True)
work = tempfile.mkdtemp(prefix="cursor-admin-")
os.chdir(work)
os.execve(AGENT, [AGENT, sys.argv[1]], cli_env("/var/lib/cursor-login"))
