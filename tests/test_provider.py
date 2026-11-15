"""Run with AstrBot's own interpreter; imports the actual Provider base/API."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.capabilities import supports_modality

from cursor_plugin import runtime
from cursor_plugin.errors import CursorError
from cursor_plugin.provider import CursorCLIProvider

ROOT = Path(__file__).resolve().parents[1]


class ProviderMockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        identity = Path(self.scratch.name) / "identity"
        known = Path(self.scratch.name) / "known_hosts"
        identity.touch()
        known.touch()
        runtime.configure(
            {
                "model_id": "mock-plan-model",
                "ssh_binary": str(ROOT / "tests/mock_ssh.py"),
                "identity_file": str(identity),
                "known_hosts_file": str(known),
                "timeout_seconds": 1,
            }
        )
        self.provider = CursorCLIProvider(
            {"id": "test-cursor", "type": "cursor_cli", "model": "mock-plan-model"}, {}
        )

    async def asyncTearDown(self):
        await self.provider.terminate()
        self.scratch.cleanup()

    async def test_native_response_and_astrbot_context(self):
        history = [
            {"role": "user", "content": "上一条"},
            {"role": "assistant", "content": "上一答"},
        ]
        before = json.dumps(history)
        reply = await self.provider.text_chat(
            prompt="当前问题",
            system_prompt="测试人格",
            contexts=history,
            session_id="must-not-be-used-as-cursor-session",
        )
        self.assertEqual(reply.role, "assistant")
        self.assertEqual(reply.completion_text, "模拟回复：上下文已接收")
        self.assertEqual(json.dumps(history), before)

    async def test_failure_categories_are_sanitized(self):
        for sentinel, code in (
            ("timeout", "timeout"),
            ("unauth", "not_logged_in"),
            ("failed", "cli_failed"),
            ("bad-json", "protocol"),
            ("overflow", "output_limit"),
        ):
            with self.subTest(case=sentinel):
                with self.assertRaises(CursorError) as caught:
                    await self.provider.text_chat(prompt="MOCK:" + sentinel)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("PRIVATE_CHAT_SENTINEL", str(caught.exception))
                self.assertNotIn("SECRET_TOKEN_SENTINEL", str(caught.exception))

    async def test_environment_is_not_inherited(self):
        with patch.dict(
            os.environ,
            {
                "ASTRBOT_SECRET": "fake",
                "OPENAI_API_KEY": "fake",
                "CURSOR_API_KEY": "fake",
                "NODE_OPTIONS": "fake",
                "SSH_AUTH_SOCK": "/tmp/fake",
            },
        ):
            result = await self.provider.text_chat(prompt="MOCK:env")
        self.assertEqual(result.completion_text, "isolated")

    async def test_stream_has_one_final_response(self):
        results = [r async for r in self.provider.text_chat_stream(prompt="MOCK:env")]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].is_chunk)

    async def test_model_is_explicit_and_never_switched(self):
        self.assertEqual(await self.provider.get_models(), ["mock-plan-model"])
        for model in ("auto", "default", "another-model", "--force", ""):
            with self.assertRaises(CursorError) as caught:
                await self.provider.text_chat(prompt="MOCK:env", model=model)
            self.assertEqual(caught.exception.code, "model")

    async def test_host_filters_tools_using_text_only_capabilities(self):
        config = {
            "id": "test-cursor",
            "type": "cursor_cli",
            "model": "mock-plan-model",
            "modalities": ["text", "image", "audio", "tool_use"],
        }
        provider = CursorCLIProvider(config, {})
        runner = SimpleNamespace(provider=provider, req=SimpleNamespace(func_tool=object()))
        try:
            self.assertIsNone(ToolLoopAgentRunner._func_tool_for_provider(runner))
            self.assertFalse(supports_modality(provider, "image"))
            self.assertFalse(supports_modality(provider, "audio"))
            self.assertTrue(supports_modality(provider, "text"))
            self.assertEqual(config["modalities"], ["text", "image", "audio", "tool_use"])
        finally:
            await provider.terminate()

    async def test_nontext_requests_are_rejected_before_cli(self):
        for args in (
            {"image_urls": ["file:///etc/passwd"]},
            {"audio_urls": ["x"]},
            {"func_tool": ["tool"]},
            {"contexts": [{"role": "tool", "content": "x"}]},
            {"extra_user_content_parts": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ):
            with self.assertRaises(CursorError) as caught:
                await self.provider.text_chat(prompt="MOCK:env", **args)
            self.assertEqual(caught.exception.code, "unsupported")

    async def test_provider_termination_cancels_running_request(self):
        task = asyncio.create_task(self.provider.text_chat(prompt="MOCK:timeout"))
        await asyncio.sleep(0.2)
        await self.provider.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(CursorError) as caught:
            await self.provider.text_chat(prompt="MOCK:env")
        self.assertEqual(caught.exception.code, "closed")
