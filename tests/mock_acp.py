#!/usr/bin/env python3
"""Offline ACP peer for the operator-run environment script; no network or login."""

import asyncio
import base64
import json
import sys


async def main():
    reader = asyncio.StreamReader()
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    sessions = {}
    running = {}
    counter = 0

    def send(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def result(request, value):
        send({"jsonrpc": "2.0", "id": request["id"], "result": value})

    async def prompt(request):
        params = request["params"]
        sid = params["sessionId"]
        try:
            text = params["prompt"][0]["text"]
            if "CANCEL_TEST" in text:
                await asyncio.sleep(10)
            else:
                await asyncio.sleep(0.05)
            chunks = ["OK"]
            if "MOCK_STREAM_FAILURE" in text:
                chunks = ["部分正常回复。", "Error: RetriableError: [unavailable] PING timed out"]
            if "MOCK_IMAGE_REQUIRED" in text:
                images = params["prompt"][1:]
                png = (
                    len(images) == 1
                    and images[0].get("type") == "image"
                    and images[0].get("mimeType") == "image/png"
                    and base64.b64decode(images[0]["data"], validate=True).startswith(b"\x89PNG")
                )
                chunks = ["INLINE-IMAGE" if png else "IMAGE-MISSING"]
            if "MOCK_NO_IMAGE" in text:
                chunks = ["NO-IMAGE" if len(params["prompt"]) == 1 else "UNEXPECTED-IMAGE"]
            for chunk in chunks:
                send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": chunk},
                            },
                        },
                    }
                )
            result(request, {"stopReason": "end_turn"})
        except asyncio.CancelledError:
            result(request, {"stopReason": "cancelled"})
        finally:
            running.pop(sid, None)

    while raw := await reader.readline():
        request = json.loads(raw)
        method = request.get("method")
        params = request.get("params", {})
        if method == "initialize":
            result(
                request,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {"promptCapabilities": {"image": True}},
                    "authMethods": [{"id": "cursor_login", "name": "Mock"}],
                },
            )
        elif method == "authenticate":
            result(request, {})
        elif method == "session/new":
            counter += 1
            sid = f"mock-session-{counter}"
            sessions[sid] = {}
            result(
                request,
                {
                    "sessionId": sid,
                    "models": {"availableModels": [{"modelId": "mock-plan-model"}]},
                    "modes": {"availableModes": [{"id": "ask"}]},
                },
            )
        elif method == "session/set_config_option":
            result(request, {"configOptions": [{"id": "model", "currentValue": params["value"]}]})
        elif method == "session/set_mode":
            assert params["modeId"] == "ask"
            result(request, {})
        elif method == "session/prompt":
            running[params["sessionId"]] = asyncio.create_task(prompt(request))
        elif method == "session/cancel":
            task = running.get(params["sessionId"])
            if task is not None:
                task.cancel()
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32601, "message": "Unsupported mock method"},
                }
            )
    for task in list(running.values()):
        task.cancel()
    await asyncio.gather(*running.values(), return_exceptions=True)


asyncio.run(main())
