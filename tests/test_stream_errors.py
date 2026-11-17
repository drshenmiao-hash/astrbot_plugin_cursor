"""Regressions for ACP error text reported as a successful end_turn.

The peer uses real ACP session/prompt assembly and update handling with an
in-memory RPC source. No Cursor, AstrBot server, SSH or child process is used.
"""

import asyncio
import json
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

from cursor_plugin import metrics
from cursor_plugin.acp import ACPClient
from cursor_plugin.engine import Engine
from cursor_plugin.errors import CursorError, CursorStreamError, retry_stream_errors
from cursor_plugin.logic import classify_reply_error
from cursor_plugin.settings import settings_from

STALL = "Error: RetriableError: Connection stalled"
PING = "Error: RetriableError: [unavailable] PING timed out"
NORMAL = [
    "OK",
    "好",
    "收到",
    "42",
    "Connection stable",
    "PING 正常",
    "Error: 输入有误",
    "RetriableError 是什么？",
    "Connection stalled 是什么意思？",
    "PING timed out 表示心跳超时。",
    "`Error: RetriableError: Connection stalled`",
    "```text\nError: RetriableError: Connection stalled\n```",
]


class ReplyClassificationTests(unittest.TestCase):
    def test_known_error_replies_with_whitespace_and_optional_error_prefix(self):
        cases = {
            STALL: "connection_stalled",
            PING: "ping_timeout",
            " \n  Error: RetriableError: Connection stalled \t\n": "connection_stalled",
            "RetriableError: [unavailable] PING timed out": "ping_timeout",
            "Connection stalled": "connection_stalled",
            "  Error: PING timed out.  ": "ping_timeout",
            "[unavailable] PING timed out": "ping_timeout",
            "RetriableError: upstream reset PRIVATE_ERROR_SENTINEL": "upstream_retryable",
        }
        for text, expected in cases.items():
            with self.subTest(reply=text):
                self.assertEqual(classify_reply_error(text), expected)

    def test_terminal_machine_error_after_partial_output_is_failure(self):
        self.assertEqual(classify_reply_error("部分回复\n" + PING), "ping_timeout")

    def test_normal_short_replies_and_explanations_are_not_errors(self):
        for text in NORMAL:
            with self.subTest(reply=text):
                self.assertIsNone(classify_reply_error(text))


class StreamPeer(ACPClient):
    def __init__(self, replies):
        self.replies = deque(replies)
        self.turns = {}
        self.session_models = {}
        self.ready = {"cwd": "/memory"}
        self.closed = False
        self.output_limit = 1024 * 1024
        self.session_count = 0
        self.prompt_count = 0

    async def call(self, method, params, *, timeout=None):
        if method == "session/new":
            self.session_count += 1
            return {
                "sessionId": f"session-{self.session_count}",
                "models": {"availableModels": [{"modelId": "grok-4.6", "name": "Grok 4.6"}]},
                "modes": {"availableModes": [{"id": "ask"}]},
            }
        if method == "session/set_config_option":
            return {"configOptions": [{"id": "model", "currentValue": params["value"]}]}
        if method == "session/set_mode":
            return {}
        if method != "session/prompt":
            raise AssertionError(method)
        self.prompt_count += 1
        text = self.replies.popleft()
        sid = params["sessionId"]
        self.turns[sid]["wire_bytes"] = len(json.dumps(params).encode())
        # Simulate chunks followed by a misleading successful end_turn.
        for start in range(0, len(text), 7):
            self._update(
                {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text[start : start + 7]},
                    },
                }
            )
        return {"stopReason": "end_turn"}

    async def close(self):
        self.closed = True


class StreamFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine(settings_from({"model_id": "grok-4.6"}))
        self.events = []
        self.old_store = metrics._usage_store
        metrics.set_usage_store(None)

        def record(event, **values):
            self.events.append({"event": event, **values})
            metrics.record(event, **values)

        records = patch("cursor_plugin.engine.record", side_effect=record)
        records.start()
        self.addCleanup(records.stop)
        process = patch(
            "asyncio.create_subprocess_exec", side_effect=AssertionError("No real process")
        )
        process.start()
        self.addCleanup(process.stop)

    async def asyncTearDown(self):
        await self.engine.close()
        metrics.set_usage_store(self.old_store)

    def peer(self, replies):
        peer = StreamPeer(replies)
        self.engine.client = peer
        return peer

    async def request(self):
        return await self.engine.submit(
            [{"role": "user", "content": "PRIVATE_USER_SENTINEL"}], "group"
        )

    def finished(self):
        return [e for e in self.events if e["event"] == "request_finished"]

    async def test_acp_error_is_failed_accounted_and_never_cached(self):
        for text, expected in [(STALL, "connection_stalled"), (PING, "ping_timeout")]:
            with self.subTest(reply=text):
                self.peer([text])
                with self.assertRaises(CursorStreamError) as caught:
                    await self.request()
                self.assertEqual(caught.exception.code, expected)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn(text, str(caught.exception))
                result = self.finished()[-1]
                self.assertEqual(result["code"], expected)
                self.assertEqual(result["output_bytes"], len(text.encode()))
                self.assertEqual(result["model_id"], "grok-4.6")
                self.assertNotIn("group", self.engine.sessions)

    async def test_retry_returns_only_normal_reply_and_uses_fresh_session(self):
        peer = self.peer([STALL, "正常回复"])
        with self.assertLogs("astrbot", level="INFO") as logs:
            reply = await retry_stream_errors(self.request)
        self.assertEqual(reply, "正常回复")
        self.assertEqual(peer.prompt_count, 2)
        self.assertEqual(peer.session_count, 2)
        self.assertEqual([e["code"] for e in self.finished()], ["connection_stalled", "ok"])
        self.assertEqual(self.engine.sessions["group"]["last_reply"], "正常回复")
        joined = "\n".join(logs.output)
        self.assertIn("connection_stalled", joined)
        self.assertNotIn(STALL, joined)
        self.assertNotIn("PRIVATE_USER_SENTINEL", joined)

    async def test_exhausted_retry_propagates_safe_business_error_for_host_fallback(self):
        peer = self.peer([STALL, PING])
        with (
            self.assertLogs("astrbot", level="INFO") as logs,
            self.assertRaises(CursorStreamError) as caught,
        ):
            await retry_stream_errors(self.request)
        self.assertEqual(peer.prompt_count, 2)
        self.assertEqual(caught.exception.code, "ping_timeout")
        self.assertNotIn("RetriableError", str(caught.exception))
        self.assertNotIn("PING timed out", str(caught.exception))
        self.assertEqual(
            [e["code"] for e in self.finished()], ["connection_stalled", "ping_timeout"]
        )
        self.assertNotIn("group", self.engine.sessions)
        self.assertIn("ping_timeout", "\n".join(logs.output))
        self.assertNotIn(PING, "\n".join(logs.output))

    async def test_zero_retry_setting_is_honored(self):
        peer = self.peer([STALL, "不应调用"])
        with self.assertLogs("astrbot", level="WARNING"), self.assertRaises(CursorStreamError):
            await retry_stream_errors(self.request, request_max_retries=0)
        self.assertEqual(peer.prompt_count, 1)
        self.assertEqual(self.finished()[0]["code"], "connection_stalled")

    async def test_large_retry_setting_is_capped_to_one_retry(self):
        peer = self.peer([PING, PING, "不应调用"])
        with self.assertLogs("astrbot", level="WARNING"), self.assertRaises(CursorStreamError):
            await retry_stream_errors(self.request, request_max_retries=999)
        self.assertEqual(peer.prompt_count, 2)
        self.assertTrue(all(e["code"] == "ping_timeout" for e in self.finished()))

    async def test_normal_short_replies_pass_unchanged_with_success_code(self):
        for text in NORMAL:
            with self.subTest(reply=text):
                # Fresh scopes avoid intentionally replaying previous responses.
                peer = self.peer([text])
                self.engine.sessions.clear()
                self.assertEqual(await retry_stream_errors(self.request), text)
                self.assertEqual(peer.prompt_count, 1)
                self.assertEqual(self.finished()[-1]["code"], "ok")

    async def test_partial_output_followed_by_error_is_not_returned(self):
        self.peer(["部分回复\n" + PING])
        with self.assertRaises(CursorStreamError) as caught:
            await self.request()
        self.assertEqual(caught.exception.code, "ping_timeout")
        self.assertEqual(self.finished()[-1]["code"], "ping_timeout")

    async def test_unrelated_failure_and_cancellation_are_not_retried(self):
        for error in [CursorError("quota_exhausted"), asyncio.CancelledError()]:
            operation = AsyncMock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await retry_stream_errors(operation)
            operation.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
