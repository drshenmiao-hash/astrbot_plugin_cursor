"""Local execution using temporary CLI bundles, real processes and synthetic login files."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_plugin.connectivity import check_environment
from cursor_plugin.credentials import DENY, write_credentials
from cursor_plugin.diagnostics import diagnose
from cursor_plugin.errors import CursorError
from cursor_plugin.launcher import ProcessOwner
from cursor_plugin.settings import settings_from

VERSION = "2026.11.22-fixture"


class LocalModes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.cli = self.bundle / "cursor-agent"
        (self.bundle / "index.js").touch()
        (self.bundle / "node").touch(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "agent").symlink_to(self.cli)
        self.auth_file = self.root / "login" / "auth.json"
        self.original = {"accessToken": "fixture-original", "refreshToken": "fixture-refresh"}
        self.with_metadata = {**self.original, "profile": {"label": "preserve-on-disk"}}
        write_credentials(self.auth_file, self.with_metadata)
        self.write_cli(f"print({VERSION!r})")
        self.cfg = settings_from(
            {
                "transport": "local_direct",
                "cursor_auth_path": str(self.auth_file),
                "bubblewrap_binary": str(self.root / "missing-bwrap"),
            }
        )

    def write_cli(self, source):
        self.cli.write_text(f"#!{sys.executable}\n{source}\n")
        self.cli.chmod(0o700)

    async def test_cli_discovery_version_and_configured_path_priority(self):
        with patch.dict(os.environ, {"PATH": str(self.bin)}):
            report = await diagnose(self.cfg)
            self.assertEqual(report["cli_path"], str(self.bin / "agent"))
            self.assertEqual(report["cli_resolved_path"], str(self.cli))
            self.assertEqual(report["cli_state"], "executable")
            self.assertEqual(report["cli_version"], VERSION)
            self.assertEqual(report["file_isolation"], "disabled")
            self.assertEqual(report["login_file"], "ok")
            missing = {**self.cfg, "cli_binary": str(self.root / "absent")}
            self.assertEqual((await diagnose(missing))["cli_state"], "not_installed")
            relative = {**self.cfg, "cli_binary": "agent"}
            self.assertEqual((await diagnose(relative))["cli_installation"], "cli_path_absolute")
            self.cli.chmod(0o600)
            self.assertEqual((await diagnose(self.cfg))["cli_installation"], "cli_not_executable")
            self.cli.chmod(0o700)
            (self.bundle / "index.js").unlink()
            self.assertEqual((await diagnose(self.cfg))["cli_installation"], "cli_layout")

    async def test_bad_cli_and_unverified_version_are_distinct(self):
        self.cfg["cli_binary"] = str(self.cli)
        self.write_cli("raise SystemExit(31)")
        self.assertEqual((await diagnose(self.cfg))["cli_state"], "incompatible")
        self.write_cli("print('fixture-private-output')")
        report = await diagnose(self.cfg)
        self.assertEqual(report["cli_state"], "version_unverified")
        self.assertNotIn("fixture-private-output", json.dumps(report))
        self.write_cli("import time\ntime.sleep(10)")
        with patch("cursor_plugin.diagnostics._VERSION_TIMEOUT", 0.1):
            report = await asyncio.wait_for(diagnose(self.cfg), 2)
        self.assertEqual(report["cli_state"], "version_unverified")

    async def test_sandbox_probe_and_legacy_auto_never_run_direct(self):
        marker = self.root / "cli-started"
        self.write_cli(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nprint({VERSION!r})"
        )
        bwrap = self.root / "bwrap"
        self.cfg.update(cli_binary=str(self.cli), transport="local", bubblewrap_binary=str(bwrap))
        result = await check_environment(self.cfg)
        self.assertEqual(result["steps"]["bubblewrap"]["status"], "failed")
        self.assertFalse(marker.exists())
        auto = {**self.cfg, "transport": "auto"}
        self.assertEqual((await diagnose(auto))["transport"], "ssh")

        bwrap.write_text("#!/bin/sh\nexit 1\n")
        bwrap.chmod(0o700)
        report = await diagnose(auto)
        self.assertEqual(report["transport"], "local")
        self.assertEqual(report["file_isolation"], "sandbox_unavailable")
        self.assertFalse(marker.exists())
        bwrap.write_text(
            '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\n'
            f'if [ "$1" = /opt/cursor/cursor-agent ]; then exec "{self.cli}" "$2"; fi\n'
            'exec "$@"\n'
        )
        report = await diagnose(self.cfg)
        self.assertEqual(report["file_isolation"], "ok")
        self.assertEqual(report["cli_version"], VERSION)
        owner = ProcessOwner(self.cfg)
        self.addAsyncCleanup(owner.close)
        process, ready = await owner.start()
        self.assertEqual(ready["transport"], "local")
        self.assertEqual(ready["cwd"], "/work")
        self.assertEqual(await process.stdout.readline(), VERSION.encode() + b"\n")

    async def test_direct_home_work_environment_and_refresh_are_scoped(self):
        outside = self.root / "outside-project"
        outside.mkdir()
        for name in ("AGENTS.md", "CLAUDE.md", ".git", ".cursor", ".agents", ".mcp.json"):
            (outside / name).write_text("outside fixture")
        self.write_cli("""
import json
import os
import sys
from pathlib import Path

home = Path.home()
cwd = Path.cwd()
auth = home / ".config/cursor/auth.json"
cfg = auth.with_name("cli-config.json")
initial = json.loads(auth.read_text())
print(json.dumps({
    "home": str(home), "cwd": str(cwd), "work_files": list(map(str, cwd.iterdir())),
    "config": json.loads(cfg.read_text()), "auth": initial, "env": dict(os.environ),
    "flags": sys.argv[1:],
}), flush=True)
auth.write_text(json.dumps({**initial, "accessToken": "fixture-new"}))
print("refresh written", flush=True)
sys.stdin.read()
""")
        self.cfg["cli_binary"] = str(self.cli)
        owner = ProcessOwner(self.cfg)
        self.addAsyncCleanup(owner.close)
        with patch.dict(
            os.environ,
            {
                "HOME": str(outside),
                "TMPDIR": str(outside),
                "XDG_CONFIG_HOME": str(outside),
                "CURSOR_API_KEY": "outside fixture",
                "CURSOR_AUTH_TOKEN": "outside fixture",
                "OPENAI_API_KEY": "outside fixture",
                "SSH_AUTH_SOCK": "outside fixture",
                "CURSOR_WORKER_MCP_CONFIG_JSON": "outside fixture",
                "PYTHONPATH": str(outside),
            },
        ):
            process, ready = await owner.start()
        raw = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        self.assertEqual(ready["transport"], "local_direct")
        self.assertEqual(ready["cwd"], raw["cwd"])
        self.assertEqual(raw["flags"], ["--disable-auto-update", "acp"])
        self.assertEqual(raw["work_files"], [])
        self.assertNotIn(str(outside), str(raw))
        self.assertEqual(raw["auth"], self.original)
        self.assertEqual(raw["config"]["permissions"], {"allow": [], "deny": DENY})
        self.assertEqual(Path(raw["home"]).parent, Path(raw["cwd"]).parent)
        self.assertEqual(Path(raw["home"]).stat().st_mode & 0o777, 0o700)
        self.assertEqual(raw["env"]["CURSOR_CONFIG_DIR"], raw["home"] + "/.config/cursor")
        self.assertEqual(raw["env"]["CURSOR_DATA_DIR"], raw["home"] + "/.cursor")
        for secret in (
            "OPENAI_API_KEY",
            "CURSOR_API_KEY",
            "CURSOR_AUTH_TOKEN",
            "SSH_AUTH_SOCK",
            "CURSOR_WORKER_MCP_CONFIG_JSON",
            "PYTHONPATH",
        ):
            self.assertNotIn(secret, raw["env"])

        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 3), b"refresh written\n")
        before = json.loads(self.auth_file.read_text())
        self.assertEqual(before, self.with_metadata)
        # A static health check can read login material while the chat holds its lock.
        result = await diagnose(self.cfg)
        self.assertEqual(result["login_file"], "ok")
        other = ProcessOwner(self.cfg)
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await other.start()
        await owner.close()
        self.assertIsNotNone(process.returncode)
        self.assertFalse(Path(raw["home"]).parent.exists())
        refreshed = {**self.with_metadata, "accessToken": "fixture-new"}
        self.assertEqual(json.loads(self.auth_file.read_text()), refreshed)

        proc, _ = await owner.start()
        await asyncio.wait_for(proc.stdout.readline(), 3)
        await asyncio.wait_for(proc.stdout.readline(), 3)
        changed = {**self.with_metadata, "accessToken": "fixture-admin-relogin"}
        write_credentials(self.auth_file, changed)
        await owner.close()
        self.assertEqual(json.loads(self.auth_file.read_text()), changed)

    async def test_direct_rejects_nonprivate_or_symlinked_login_file(self):
        self.cfg["cli_binary"] = str(self.cli)
        self.auth_file.chmod(0o644)
        self.assertEqual((await diagnose(self.cfg))["login_file"], "auth_file_permissions")
        owner = ProcessOwner(self.cfg)
        with self.assertRaisesRegex(CursorError, "auth_file_permissions"):
            await owner.start()
        real = self.auth_file.rename(self.auth_file.with_suffix(".real"))
        self.auth_file.symlink_to(real)
        self.assertEqual((await diagnose(self.cfg))["login_file"], "config")
