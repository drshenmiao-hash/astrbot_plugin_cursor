"""Exercise runner cleanup through a local stand-in for the SSH channel."""

import asyncio
import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_plugin import runtime
from cursor_plugin.engine import Engine
from cursor_plugin.errors import CursorError

ROOT = Path(__file__).resolve().parents[1]


class RemoteCleanup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.mode = self.root / "mode"
        self.mode.write_text("normal")
        self.auth = self.root / "fixture-auth.json"
        self.auth.write_text('{"accessToken":"fixture"}')
        self.lock = self.root / "fixture.lock"
        self.pids = self.root / "pids.jsonl"
        self.catalog = self.root / "catalog"

        # The simulated worker detaches from the ACP process group and retains stdout.
        agent = self.root / "cursor-agent"
        bootstrap = f"""#!{sys.executable}
import subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         start_new_session=True)
with open({str(self.pids)!r}, "a") as audit:
    audit.write(str([__import__('os').getpid(), child.pid]) + "\\n")
mode = Path({str(self.mode)!r}).read_text()
"""
        source = (ROOT / "tests/mock_acp.py").read_text()
        source = source[source.index("import asyncio") :]
        source = source.replace(
            'elif method == "session/new":',
            f"""elif method == "session/new":
            Path({str(self.catalog)!r}).touch()
            if mode == "hang":
                await asyncio.sleep(300)
            if mode == "reject":
                send({{"jsonrpc":"2.0", "id": request["id"],
                      "error": {{"code": -32000, "message": "fixture"}}}})
                continue""",
        )
        agent.write_text(bootstrap + source)
        agent.chmod(0o700)

        ssh = self.root / "ssh-fixture.py"
        ssh.write_text(
            f"""import os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from cursor_plugin import runner
mode = Path({str(self.mode)!r}).read_text()
runner.AGENT = {str(agent)!r}
runner.AUTH = Path({str(self.auth)!r})
runner.LOCK = {str(self.lock)!r}
runner.static_health = lambda: {{"image_sha": "ok", "cli_version": "fixture",
                                 "cleanup_protocol": 0 if mode == "legacy" else 1}}
if sys.argv[1:] == ["diagnose"]:
    os.environ["SSH_ORIGINAL_COMMAND"] = "diagnose"
code = runner.main()
raise SystemExit(0 if mode == "missing_ack" and code == 42 else code)
"""
        )
        self.enterContext(
            patch("cursor_plugin.launcher.ssh_argv", return_value=[sys.executable, str(ssh)])
        )
        names = (
            "_settings",
            "_engine",
            "_available_models",
            "_startup_failure",
            "_validation_task",
            "_retry_validation_after",
            "_validation_stopped",
        )
        self.enterContext(
            patch.multiple(runtime, **{name: getattr(runtime, name) for name in names})
        )
        self.enterContext(patch.object(runtime, "_STARTUP_BACKOFF", (0, 0)))
        runtime._engine = None
        runtime.configure({"transport": "ssh", "model_id": "mock-plan-model"})
        self.addAsyncCleanup(runtime.stop_validation)

    def process_ids(self):
        return [pid for line in self.pids.read_text().splitlines() for pid in json.loads(line)]

    def assert_released(self):
        for pid in self.process_ids():
            with self.assertRaises(ProcessLookupError, msg=f"runner child {pid} was not reaped"):
                os.kill(pid, 0)
        with self.lock.open("a+b") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def wait_for_catalog(self):
        async with asyncio.timeout(3):
            while not self.catalog.exists():
                await asyncio.sleep(0.01)

    async def test_validate_then_generate_with_separate_runner_and_lock(self):
        with self.assertLogs("astrbot", level="INFO") as logs:
            await runtime.ensure_model_validated(startup=True)
        self.assertIn("process_cleanup_verified", "\n".join(logs.output))
        self.assert_released()
        engine = Engine(runtime.settings())
        self.addAsyncCleanup(engine.close)
        self.assertEqual(await engine.submit([{"role": "user", "content": "hello"}], "test"), "OK")
        self.assertEqual(len(self.process_ids()), 4)
        await engine.close()
        self.assert_released()

    async def test_catalog_error_and_timeout_release_detached_children(self):
        self.mode.write_text("reject")
        with self.assertRaisesRegex(CursorError, "not_logged_in"):
            await runtime._fetch_available_models()
        self.assert_released()
        self.catalog.unlink()
        self.mode.write_text("hang")
        runtime._settings["queue_timeout_seconds"] = 1
        with self.assertRaises(TimeoutError):
            await runtime._fetch_available_models()
        self.assertTrue(self.catalog.exists())
        self.assert_released()

    async def test_cancelled_probe_and_plugin_reload_cleanup_release_the_lock(self):
        self.mode.write_text("hang")
        probe = asyncio.create_task(runtime._fetch_available_models())
        await self.wait_for_catalog()
        probe.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await probe
        self.assert_released()

        self.catalog.unlink()
        pending = asyncio.create_task(runtime.ensure_model_validated(startup=True))
        await self.wait_for_catalog()
        # This is the same validation teardown the plugin awaits during reload.
        await runtime.stop_validation()
        with self.assertRaisesRegex(CursorError, "closed"):
            await pending
        self.assert_released()

    async def test_missing_confirmation_is_failure_not_model_readiness(self):
        self.mode.write_text("missing_ack")
        with self.assertLogs("astrbot", level="INFO") as logs:
            with self.assertRaisesRegex(CursorError, "cleanup_unverified"):
                await runtime.ensure_model_validated(startup=True)
        self.assert_released()
        self.assertEqual(runtime.settings()["model"], "")
        self.assertEqual(runtime._startup_failure, "cleanup_unverified")
        self.assertEqual(len(self.process_ids()), 2)
        self.assertNotIn("process_cleanup_verified", "\n".join(logs.output))

    async def test_legacy_runner_never_starts_an_unverifiable_acp_probe(self):
        self.mode.write_text("legacy")
        with self.assertRaisesRegex(CursorError, "runner_cleanup_unsupported"):
            await runtime.ensure_model_validated(startup=True)
        self.assertFalse(self.pids.exists())

    async def test_busy_rechecks_after_lock_release_without_a_restart(self):
        with self.lock.open("a+b") as blocker:
            fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(CursorError, "runner_in_use"):
                await runtime.ensure_model_validated(startup=True)
            self.assertFalse(self.pids.exists())
        runtime._retry_validation_after = 0
        await asyncio.gather(*(runtime.ensure_model_validated() for _ in range(4)))
        self.assertEqual(runtime.settings()["model"], "mock-plan-model")
        self.assertEqual(len(self.process_ids()), 2)
        self.assert_released()

    async def test_borrowed_live_engine_is_not_closed_or_rotated_by_validation(self):
        runtime._settings["model"] = "mock-plan-model"
        engine = Engine(runtime.settings())
        self.addAsyncCleanup(engine.close)
        runtime._engine = engine
        first = [{"role": "user", "content": "first"}]
        self.assertEqual(await engine.submit(first, "test"), "OK")
        client = engine.client
        active = asyncio.create_task(
            engine.submit([{"role": "user", "content": "CANCEL_TEST"}], "active")
        )
        async with asyncio.timeout(3):
            while not client.turns:
                await asyncio.sleep(0.01)
        catalog = await runtime._fetch_available_models()
        self.assertEqual(catalog, [{"modelId": "mock-plan-model"}])
        self.assertIs(engine.client, client)
        self.assertFalse(active.done())
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        with self.lock.open("a+b") as contender:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine.session_slots = engine.cfg["max_sessions"]
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await runtime._fetch_available_models()
        self.assertIs(engine.client, client)
        history = first + [
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "next"},
        ]
        self.assertEqual(await engine.submit(history, "test"), "OK")
        self.assertEqual(len(self.process_ids()), 2)
        await engine.close()
        self.assert_released()

    async def test_chat_waits_for_temporary_busy_and_caps_its_attempts(self):
        from unittest.mock import AsyncMock

        runtime._settings["model"] = "mock-plan-model"
        engine = Engine(runtime.settings())
        self.addAsyncCleanup(engine.close)
        get_client = engine._new_client
        occupied = asyncio.Event()

        async def observe():
            try:
                await get_client()
            except CursorError as exc:
                if exc.code == "runner_in_use":
                    occupied.set()
                raise

        message = [{"role": "user", "content": "chat"}]
        with patch.object(engine, "_new_client", side_effect=observe) as start:
            with self.lock.open("a+b") as blocker:
                fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                chat = asyncio.create_task(engine.submit(message, "chat"))
                await asyncio.wait_for(occupied.wait(), 3)
            self.assertEqual(await asyncio.wait_for(chat, 3), "OK")
            self.assertEqual(start.await_count, 2)
        await engine.close()
        self.assert_released()

        engine = Engine(runtime.settings())
        self.addAsyncCleanup(engine.close)
        always_busy = AsyncMock(side_effect=CursorError("runner_in_use"))
        with patch.object(engine, "_new_client", always_busy):
            results = await asyncio.gather(
                *(engine.submit(message, str(i)) for i in range(4)), return_exceptions=True
            )
        self.assertEqual([exc.code for exc in results], ["runner_in_use"] * 4)
        self.assertEqual(always_busy.await_count, 3)
