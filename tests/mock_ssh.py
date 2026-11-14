#!/usr/bin/python3
"""Fake SSH process exercising the same protocol and real CLI subprocess code."""

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cursor_plugin.errors import CursorError
from cursor_plugin.protocol import VERSION, decode, encode
from cursor_plugin.runner import complete

assert "StrictHostKeyChecking=yes" in sys.argv
assert "IdentityAgent=none" in sys.argv
request = decode(sys.stdin.buffer.read())
with TemporaryDirectory(prefix="cursor-mock-auth-") as folder:
    auth = Path(folder) / "auth.json"
    auth.write_text(
        json.dumps({"accessToken": "fake-only-not-valid", "refreshToken": "fake-refresh-not-valid"})
    )
    try:
        answer = asyncio.run(
            complete(
                request,
                agent=str(ROOT / "tests/mock_cli.py"),
                auth_path=auth,
                allowed_model="mock-plan-model",
            )
        )
        response = {"version": VERSION, "ok": True, "text": answer}
    except CursorError as exc:
        response = {"version": VERSION, "ok": False, "code": exc.code}
sys.stdout.buffer.write(encode(response))
