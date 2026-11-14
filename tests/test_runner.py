"""Dependency-free boundary tests. No official CLI is executed."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from cursor_plugin.errors import CursorError
from cursor_plugin.process import run_process
from cursor_plugin.protocol import MAX_CONTEXT
from cursor_plugin.runner import cli_reply, complete
from cursor_plugin.transcript import build_transcript

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_login_is_reported_without_starting_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CursorError) as caught:
                await complete(
                    {"model": "mock-plan-model", "prompt": "x", "timeout": 1},
                    agent="/never-execute",
                    auth_path=Path(folder) / "missing",
                    allowed_model="mock-plan-model",
                )
        self.assertEqual(caught.exception.code, "not_logged_in")

    async def test_api_credentials_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.json"
            path.write_text(json.dumps({"accessToken": "fake", "apiKey": "fake"}))
            with self.assertRaises(CursorError) as caught:
                await complete(
                    {"model": "mock-plan-model", "prompt": "x", "timeout": 1},
                    agent="/never-execute",
                    auth_path=path,
                    allowed_model="mock-plan-model",
                )
            self.assertEqual(caught.exception.code, "config")

    async def test_parent_model_pin(self):
        with self.assertRaises(CursorError) as caught:
            await complete(
                {"model": "other-model", "prompt": "x", "timeout": 1},
                agent="/never-execute",
                allowed_model="mock-plan-model",
            )
        self.assertEqual(caught.exception.code, "model")

    async def test_symlink_auth_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.json"
            path.symlink_to("/etc/passwd")
            with self.assertRaises(CursorError) as caught:
                await complete(
                    {"model": "mock-plan-model", "prompt": "x", "timeout": 1},
                    agent="/never-execute",
                    auth_path=path,
                    allowed_model="mock-plan-model",
                )
        self.assertEqual(caught.exception.code, "config")

    async def test_process_timeout_and_descendant_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "spawn.py"
            script.write_text(
                "import os,time\nfrom pathlib import Path\npid=os.fork()\nif pid == 0:\n time.sleep(2)\n Path('survived').touch()\nelse:\n time.sleep(10)\n"
            )
            with self.assertRaises(CursorError) as caught:
                await run_process(
                    ["/usr/bin/python3", str(script)],
                    b"",
                    cwd=folder,
                    env={"PATH": "/usr/bin:/bin"},
                    timeout=0.1,
                    output_limit=1024,
                )
            self.assertEqual(caught.exception.code, "timeout")
            await asyncio.sleep(2.1)
            self.assertFalse((Path(folder) / "survived").exists())

    async def test_context_limit_does_not_silently_drop_history(self):
        with self.assertRaises(CursorError) as caught:
            build_transcript(prompt="中" * MAX_CONTEXT)
        self.assertEqual(caught.exception.code, "context_limit")

    async def test_cli_result_validation(self):
        for raw in (
            b"not-json",
            b"{}",
            b'{"type":"result","subtype":"success","is_error":false,"result":""}',
        ):
            with self.assertRaises(CursorError) as caught:
                cli_reply(0, raw, b"")
            self.assertEqual(caught.exception.code, "protocol")
