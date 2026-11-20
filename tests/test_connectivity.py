"""Connectivity regressions using plugin APIs, temporary SQLite and an offline ACP process."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cursor_plugin import connectivity, metrics, web
from cursor_plugin.acp import ACPClient
from cursor_plugin.engine import Engine
from cursor_plugin.errors import CursorError
from cursor_plugin.settings import settings_from
from cursor_plugin.usage import UsageStore
from tests import test_usage_dashboard
from tests.mock_runtime import MockOwner
from tests.test_usage import event

LOCAL = {
    "transport": "local",
    "file_isolation": "ok",
    "cli_installation": "ok",
    "cli_version": "2026.03.27-mock",
    "login_file": "ok",
}


class TestConnection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = settings_from({"model_id": "mock-plan-model", "transport": "local"})
        self.processes = []
        self.calls = []
        self.cancels = []
        self.kind = "local"

        class Owner(MockOwner):
            async def start(owner):
                process, ready = await super().start()
                owner.kind = ready["transport"] = self.kind
                self.processes.append(process)
                return process, ready

        def probe(cfg):
            return Engine(cfg, owner_factory=Owner)

        call = ACPClient.call
        notify = ACPClient.notify

        async def observe(client, method, params, **kwargs):
            self.calls.append(method)
            return await call(client, method, params, **kwargs)

        async def observe_notifications(client, method, params):
            self.cancels.append(method)
            return await notify(client, method, params)

        self.enterContext(patch.object(connectivity, "Engine", side_effect=probe))
        self.enterContext(patch.object(ACPClient, "call", observe))
        self.enterContext(patch.object(ACPClient, "notify", observe_notifications))
        self.doctor = self.enterContext(
            patch.object(connectivity, "diagnose", AsyncMock(return_value=LOCAL))
        )

    async def test_check_calls_only_initialize_and_authenticate(self):
        data = await connectivity.check_connection(self.cfg)
        self.assertEqual(self.calls, ["initialize", "authenticate"])
        self.assertEqual(
            {k: item["status"] for k, item in data["steps"].items()},
            {"runner": "passed", "cli": "passed", "acp": "passed", "auth": "unknown"},
        )
        self.doctor.assert_awaited_once_with(self.cfg)
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_check_uses_read_only_live_status_and_manual_test_skips_busy(self):
        live = {"transport": "local", "handshake": True, "authenticated": True}
        result = await connectivity.check_connection(self.cfg, live=live)
        test = await connectivity.test_model(self.cfg, live=live)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.processes, [])
        self.assertIn("读取现有连接", result["steps"]["acp"]["reason"])
        self.assertEqual((test["code"], test["status"]), ("runner_in_use", "skipped"))

    async def test_ssh_handoff_with_redacted_auth_failure(self):
        self.cfg["transport"] = self.kind = "ssh"
        self.doctor.return_value = {
            "transport": "ssh",
            "ssh_connection": "ok",
            "cli_executable": "ok",
            "login_volume": "ok",
            "image_sha": "ok",
            "runner_cleanup_protocol": "ok",
            "cli_version": "PRIVATE_DATA",
            "raw_probes": {"stderr": "PRIVATE_RUNNER_INFO"},
        }
        call = ACPClient.call

        async def auth_fails(client, method, params, **kwargs):
            if method == "authenticate":
                raise CursorError("not_logged_in")
            return await call(client, method, params, **kwargs)

        with patch.object(ACPClient, "call", auth_fails):
            data = await connectivity.check_connection(self.cfg)
        self.assertEqual(data["transport"], "ssh")
        self.assertEqual(data["steps"]["acp"]["status"], "passed")
        self.assertEqual(data["steps"]["auth"]["status"], "failed")
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertEqual(self.calls, ["initialize"])
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_missing_login_skips_handshake_and_wrong_protocol_is_a_failure(self):
        self.doctor.return_value = {**LOCAL, "login_file": "not_logged_in"}
        data = await connectivity.check_connection(self.cfg)
        self.assertEqual(data["steps"]["acp"]["status"], "skipped")
        self.assertEqual(data["steps"]["auth"]["status"], "failed")
        self.assertEqual(self.calls, [])
        self.doctor.return_value = LOCAL
        with patch.object(ACPClient, "call", AsyncMock(return_value={"protocolVersion": 2})):
            data = await connectivity.check_connection(self.cfg)
        self.assertEqual(data["steps"]["acp"]["status"], "failed")
        self.assertEqual(data["steps"]["auth"]["status"], "unknown")
        self.assertIsNotNone(self.processes[0].returncode)

    async def test_manual_timeout_cancels_and_closes_own_process_then_recovers(self):
        prompt = ACPClient.prompt

        async def stall(client, session, text, **kwargs):
            self.assertEqual(text, "Reply with OK.")
            return await prompt(client, session, "CANCEL_TEST", **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            store = UsageStore(directory)
            self.addCleanup(store.close)
            self.enterContext(patch.object(metrics, "_usage_store", store))
            store.record(event())
            before = store.dashboard(30)["recent_request"]
            with (
                patch.object(ACPClient, "prompt", stall),
                patch.object(connectivity, "_TEST_TIMEOUT", 0.4),
            ):
                timed_out = await connectivity.test_model(self.cfg)
            self.assertEqual(timed_out["code"], "timeout")
            self.assertEqual(timed_out["model_id"], "mock-plan-model")
            self.assertEqual(self.cancels, ["session/cancel"])
            self.assertIsNotNone(self.processes[0].returncode)
            ok = await connectivity.test_model(self.cfg)
            self.assertEqual((ok["status"], ok["model_id"]), ("passed", "mock-plan-model"))
            self.assertIsNotNone(self.processes[1].returncode)
            self.assertEqual(self.calls.count("session/prompt"), 2)
            self.assertEqual(store.dashboard(30)["recent_request"], before)
            self.assertEqual(store.dashboard(30)["totals"]["requests"], 1)
            self.assertNotIn("text", ok)

    async def test_manual_resolves_configured_alias_only_without_validated_model(self):
        self.cfg["requested_model"] = "cursor-mock-plan-model"
        self.cfg["model"] = ""
        result = await connectivity.test_model(self.cfg)
        self.assertEqual((result["status"], result["model_id"]), ("passed", "mock-plan-model"))

        self.cfg["model"] = self.cfg["requested_model"]
        self.calls.clear()
        result = await connectivity.test_model(self.cfg)
        self.assertEqual((result["status"], result["code"]), ("failed", "model_unavailable"))
        self.assertNotIn("session/prompt", self.calls)

    async def test_manual_uses_configured_model_and_no_retry_on_missing_model(self):
        self.cfg["model"] = self.cfg["requested_model"] = "unavailable-model"
        result = await connectivity.test_model(self.cfg)
        self.assertEqual((result["code"], result["status"]), ("model_unavailable", "failed"))
        self.assertNotIn("session/prompt", self.calls)
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].returncode)


class TestRecentRequest(unittest.TestCase):
    def test_migrates_old_rows_and_excludes_cached_replies(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / "usage.sqlite3") as db:
                db.execute("""CREATE TABLE requests (
                    request_id TEXT PRIMARY KEY, day TEXT NOT NULL, session TEXT NOT NULL,
                    model_id TEXT NOT NULL, code TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    queue_ms INTEGER NOT NULL, input_bytes INTEGER NOT NULL,
                    sent_input_bytes INTEGER NOT NULL, wire_bytes INTEGER NOT NULL,
                    output_bytes INTEGER NOT NULL, process_reused INTEGER NOT NULL,
                    reused INTEGER NOT NULL, reset_reason TEXT NOT NULL
                )""")
                db.execute(
                    "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "old",
                        "2026-01-02",
                        "",
                        "",
                        "image_limit",
                        12,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        "preflight",
                    ),
                )
            store = UsageStore(directory)
            self.addCleanup(store.close)
            old = store.dashboard(7)["recent_request"]
            self.assertEqual((old["day"], old["finished_at"]), ("2026-01-02", None))
            self.assertEqual(old["outcome"], "图片预检拒绝，Cursor 请求未发送")
            store.record(event("cached", wire_bytes=0, reset_reason="cached_reply"))
            self.assertEqual(store.dashboard(7)["recent_request"], old)
            when = datetime(2026, 1, 3, 12, 34, 56, tzinfo=timezone.utc)
            store.record(event("sent", transport="ssh"), at=when)
            recent = store.dashboard(7)["recent_request"]
            self.assertEqual(recent["stage"], "success")
            self.assertEqual(recent["model_id"], "grok-4.6")
            self.assertEqual(recent["transport"], "ssh")
            self.assertEqual(recent["finished_at"], "2026-01-03T12:34:56.000+00:00")

    def test_stages_report_known_phase_and_no_unconfirmed_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UsageStore(directory)
            self.addCleanup(store.close)
            self.assertIsNone(store.dashboard(7)["recent_request"])
            for index, (code, stage, delivery) in enumerate(
                [
                    ("sandbox_unavailable", "runner", "not_sent"),
                    ("protocol", "handshake", "not_sent"),
                    ("not_logged_in", "auth", "not_sent"),
                    ("ping_timeout", "model", "sent"),
                ]
            ):
                store.record(
                    event(str(index), code=code, stage=stage, delivery=delivery, fallback="PRIVATE")
                )
                recent = store.dashboard(7)["recent_request"]
                self.assertEqual(recent["stage"], stage)
                self.assertEqual(recent["delivery"], delivery)
                self.assertNotIn("fallback", recent)
                self.assertNotIn("PRIVATE", json.dumps(recent))


class TestDiagnosticsAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_usage_dashboard.DashboardAccess.asyncSetUp(self)
        self.context.get_config = Mock(return_value={"dashboard": {"username": "admin"}})

    async def test_actions_require_admin_and_reject_extra_prompt_fields(self):
        self.request.json.return_value = {"action": "test"}
        with patch.object(self.handler, "_diagnose", new_callable=AsyncMock) as diagnose:
            for user in (None, "api_key:example", "somebody"):
                self.request.username = user
                _, status, _ = await self.handler.handle()
                self.assertEqual(status, 403)
            self.request.username = "admin"
            self.request.json.return_value = {"action": "test", "prompt": "anything"}
            _, status, _ = await self.handler.handle()
            self.assertEqual(status, 400)
            diagnose.assert_not_awaited()

    async def test_server_guard_and_unload_wait_for_cleanup(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        async def block(action):
            self.assertEqual(action, "check")
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                closed.set()

        self.request.json.return_value = {"action": "check"}
        with patch.object(self.handler, "_diagnose", side_effect=block):
            running = asyncio.create_task(self.handler.handle())
            await asyncio.wait_for(entered.wait(), 1)
            self.request.json.return_value = {"action": "test"}
            _, status, _ = await self.handler.handle()
            self.assertEqual(status, 409)
            self.handler.close()
            await self.handler.stop_diagnostics()
            self.assertTrue(closed.is_set())
            _, status, _ = await running
            self.assertEqual(status, 503)

    async def test_unload_while_reading_request_body_does_not_start_diagnostics(self):
        reading, resume = asyncio.Event(), asyncio.Event()

        async def read_body(**kwargs):
            reading.set()
            await resume.wait()
            return {"action": "test"}

        self.request.json.side_effect = read_body
        with patch.object(self.handler, "_diagnose", new_callable=AsyncMock) as diagnose:
            handling = asyncio.create_task(self.handler.handle())
            await asyncio.wait_for(reading.wait(), 1)
            self.handler.close()
            await self.handler.stop_diagnostics()
            resume.set()
            _, status, _ = await handling
        self.assertEqual(status, 503)
        diagnose.assert_not_awaited()

    async def test_admin_dispatches_current_settings_with_no_cache(self):
        cfg = settings_from({"model_id": "grok-4.6"})
        expected = {"status": "passed", "model_id": "grok-4.6"}
        self.request.json.return_value = {"action": "test"}
        with (
            patch.object(web, "settings", return_value=cfg),
            patch.object(web, "live_connection", return_value=None),
            patch.object(web, "test_model", AsyncMock(return_value=expected)) as model,
        ):
            data, status, headers = await self.handler.handle()
        self.assertEqual(status, 200)
        self.assertEqual(data["data"], expected)
        self.assertEqual(headers["Cache-Control"], "no-store")
        model.assert_awaited_once_with(cfg, live=None)
