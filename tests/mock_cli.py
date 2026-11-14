#!/usr/bin/python3
"""Fake CLI: never uses a network or real credentials."""

import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
assert args == [
    "--print",
    "--mode",
    "ask",
    "--model",
    "mock-plan-model",
    "--output-format",
    "json",
    "--trust",
]
assert not list(Path.cwd().iterdir()), "workspace must be empty"
assert Path(os.environ["HOME"]).is_dir()
assert not any(
    k in os.environ
    for k in (
        "ASTRBOT_SECRET",
        "OPENAI_API_KEY",
        "CURSOR_API_KEY",
        "SSH_AUTH_SOCK",
        "NODE_OPTIONS",
        "PYTHONPATH",
    )
)
cfg = json.loads((Path(os.environ["CURSOR_CONFIG_DIR"]) / "cli-config.json").read_text())
assert cfg["approvalMode"] == "allowlist"
assert cfg["permissions"]["allow"] == []
assert "Read(/**)" in cfg["permissions"]["deny"]
prompt = sys.stdin.read()
if "MOCK:timeout" in prompt:
    time.sleep(10)
elif "MOCK:unauth" in prompt:
    sys.stderr.write("Authentication required. PRIVATE_CHAT_SENTINEL SECRET_TOKEN_SENTINEL")
    sys.exit(1)
elif "MOCK:failed" in prompt:
    sys.stderr.write("upstream failure PRIVATE_CHAT_SENTINEL SECRET_TOKEN_SENTINEL")
    sys.exit(23)
elif "MOCK:bad-json" in prompt:
    print("PRIVATE_CHAT_SENTINEL SECRET_TOKEN_SENTINEL")
    sys.exit(0)
elif "MOCK:overflow" in prompt:
    sys.stdout.write("x" * (3 * 1024 * 1024))
    sys.exit(0)
elif "MOCK:env" in prompt:
    print(
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "isolated"}
        )
    )
    sys.exit(0)
messages = json.loads(prompt.split("\n\n", 1)[1])
assert messages == [
    {"role": "system", "content": "测试人格"},
    {"role": "user", "content": "上一条"},
    {"role": "assistant", "content": "上一答"},
    {"role": "user", "content": "当前问题"},
]
print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "模拟回复：上下文已接收",
        }
    )
)
