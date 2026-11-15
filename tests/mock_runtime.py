"""Process owner for --mock only; never available as a plugin configuration option."""

import asyncio
import os
import signal
import sys
from pathlib import Path


class MockOwner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.kind = "mock"

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(Path(__file__).with_name("mock_acp.py")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=max(self.cfg["context_max_bytes"] * 2, self.cfg["output_max_bytes"]) + 65536,
        )
        return self.process, {
            "cwd": "/mock-work",
            "transport": "mock",
            "agent_pid": self.process.pid,
        }

    def sync_credentials(self):
        pass

    async def close(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(self.process.wait(), 3)
            self.process = None
