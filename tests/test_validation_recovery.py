"""Bounded validation retries and on-demand recovery without live CLI access."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cursor_plugin import runtime
from cursor_plugin.acp import ACPClient
from cursor_plugin.errors import CursorError
from cursor_plugin.launcher import ProcessOwner
from cursor_plugin.settings import settings_from


class ValidationRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        saved = {
            name: getattr(runtime, name)
            for name in (
                "_settings",
                "_engine",
                "_available_models",
                "_startup_failure",
                "_validation_task",
                "_retry_validation_after",
                "_validation_stopped",
            )
        }
        self.enterContext(patch.multiple(runtime, **saved))
        self.enterContext(patch.object(runtime, "_STARTUP_BACKOFF", (0.001, 0.002)))
        runtime.configure({"model_id": "grok-4.6"})
        self.catalog = [{"modelId": "grok-4.6"}]
        self.fetch = self.enterContext(
            patch.object(runtime, "_fetch_available_models", new_callable=AsyncMock)
        )

    async def test_busy_at_startup_recovers_from_real_catalog_shape(self):
        self.fetch.side_effect = [
            CursorError("runner_in_use"),
            CursorError("ssh_unreachable"),
            self.catalog,
        ]
        await runtime.ensure_model_validated(startup=True)
        self.assertEqual(self.fetch.await_count, 3)
        self.assertEqual(runtime.resolve_model_id("grok-4.6"), "grok-4.6")
        self.assertIsNone(runtime._startup_failure)

    async def test_persistent_busy_is_capped_then_shared_across_requesters(self):
        self.fetch.side_effect = CursorError("runner_in_use")
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await runtime.ensure_model_validated(startup=True)
        self.assertEqual(self.fetch.await_count, 3)
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await runtime.ensure_model_validated()
        self.fetch.side_effect = None
        self.fetch.return_value = self.catalog
        runtime._retry_validation_after = 0
        await asyncio.gather(*(runtime.ensure_model_validated() for _ in range(8)))
        self.assertEqual(self.fetch.await_count, 4)
        self.assertIsNone(runtime._startup_failure)

    async def test_startup_budget_bounds_network_retries_and_leaves_recovery_available(self):
        runtime.configure({"model_id": "grok-4.6", "queue_timeout_seconds": 3600})
        cleaned = asyncio.Event()

        async def blocked_catalog():
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        self.fetch.side_effect = blocked_catalog
        with patch.object(runtime, "_STARTUP_VALIDATION_BUDGET", 0.025):
            with self.assertRaisesRegex(CursorError, "startup_timeout"):
                await asyncio.wait_for(runtime.ensure_model_validated(startup=True), 1)
        self.assertTrue(cleaned.is_set())
        self.fetch.assert_awaited_once()
        self.assertIsNone(runtime._validation_task)

        self.fetch.side_effect = None
        self.fetch.return_value = self.catalog
        runtime._retry_validation_after = 0
        await runtime.ensure_model_validated()
        self.assertEqual(runtime.resolve_model_id("grok-4.6"), "grok-4.6")

    async def test_cancellation_does_not_cancel_shared_validation(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def available():
            started.set()
            await finish.wait()
            return self.catalog

        self.fetch.side_effect = available
        first = asyncio.create_task(runtime.ensure_model_validated())
        await started.wait()
        second = asyncio.create_task(runtime.available_model_ids())
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        finish.set()
        self.assertEqual(await second, ["grok-4.6"])
        self.fetch.assert_awaited_once()

    async def test_authentication_and_missing_model_fail_without_automatic_retries(self):
        for code in ("not_logged_in", "model_unavailable"):
            with self.subTest(code=code):
                runtime.configure({"model_id": "grok-4.6"})
                self.fetch.reset_mock()
                self.fetch.side_effect = CursorError(code) if code == "not_logged_in" else None
                self.fetch.return_value = [{"modelId": "other"}]
                with self.assertRaisesRegex(CursorError, code):
                    await runtime.ensure_model_validated(startup=True)
                runtime._retry_validation_after = 0
                if code == "model_unavailable":
                    self.assertEqual(await runtime.available_model_ids(), ["other"])
                else:
                    with self.assertRaisesRegex(CursorError, code):
                        await runtime.available_model_ids()
                self.fetch.assert_awaited_once()

    async def test_recovery_failure_restarts_cooldown(self):
        self.fetch.side_effect = CursorError("runner_in_use")
        runtime.record_startup_failure("runner_in_use")
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await runtime.ensure_model_validated()
        with self.assertRaisesRegex(CursorError, "runner_in_use"):
            await runtime.ensure_model_validated()
        self.fetch.assert_awaited_once()

    async def test_stop_waits_for_probe_cleanup_and_releases_waiters_as_closed(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def probe():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        self.fetch.side_effect = probe
        caller = asyncio.create_task(runtime.ensure_model_validated())
        await started.wait()
        await runtime.stop_validation()
        self.assertTrue(cleaned.is_set())
        with self.assertRaisesRegex(CursorError, "closed"):
            await caller
        self.assertIsNone(runtime._validation_task)


class LocalLockRelease(unittest.IsolatedAsyncioTestCase):
    async def test_close_exits_process_and_releases_real_flock(self):
        import sys

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dummy_auth = root / "fixture-auth.json"
            dummy_auth.write_text('{"accessToken":"offline-fixture"}')
            cfg = settings_from({"transport": "local", "cursor_auth_path": str(dummy_auth)})
            peer = Path(__file__).with_name("mock_acp.py")
            argv = [sys.executable, "-u", str(peer)]
            with (
                patch("cursor_plugin.launcher.local_bundle", return_value=root),
                patch("cursor_plugin.launcher.bwrap_argv", return_value=argv),
            ):
                first = ProcessOwner(cfg)
                self.addAsyncCleanup(first.close)
                second = ProcessOwner(cfg)
                self.addAsyncCleanup(second.close)
                process, ready = await first.start()
                client = ACPClient(process, frame_limit=65536, output_limit=65536, ready=ready)
                self.addAsyncCleanup(client.close)
                await client.start()
                with self.assertRaisesRegex(CursorError, "runner_in_use"):
                    await second.start()
                await first.close()
                self.assertIsNotNone(process.returncode)
                successor, ready = await second.start()
                second_client = ACPClient(
                    successor, frame_limit=65536, output_limit=65536, ready=ready
                )
                self.addAsyncCleanup(second_client.close)
                await second_client.start()
                self.assertEqual(
                    await second_client.available_models(), [{"modelId": "mock-plan-model"}]
                )
                self.assertEqual(dummy_auth.read_text(), '{"accessToken":"offline-fixture"}')
