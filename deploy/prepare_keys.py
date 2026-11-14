#!/usr/bin/env python3
"""Operator-run setup. Never overwrites keys and never connects to Cursor."""

import os
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
client = root / "client"
server = root / "server"
if client.exists() or server.exists():
    raise SystemExit("client/ or server/ already exists; refusing to overwrite keys.")
os.umask(0o077)
client.mkdir(mode=0o700)
server.mkdir(mode=0o755)
os.chmod(server, 0o755)
for path in (client / "id_ed25519", server / "ssh_host_ed25519_key"):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
public = (client / "id_ed25519.pub").read_text().strip()
(server / "authorized_keys").write_text("restrict " + public + "\n")
os.chmod(server / "authorized_keys", 0o644)
host_key = (server / "ssh_host_ed25519_key.pub").read_text().split()
(client / "known_hosts").write_text("[cursor-cli]:2222 " + " ".join(host_key[:2]) + "\n")
os.chmod(client / "known_hosts", 0o600)
print("Created dedicated client/server keys and pinned known_hosts. Keep server/ root-owned.")
print("If AstrBot runs as non-root, chown client/ to that container UID before mounting.")
