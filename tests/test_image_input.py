"""Native Provider through ACP framing, with an in-memory peer and real images."""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cursor_plugin import images, runtime
from cursor_plugin.acp import ACPClient
from cursor_plugin.errors import CursorError
from cursor_plugin.provider import CursorCLIProvider
from tests.check_cursor_images import make_card

MODEL = "mock-vision-model"


class ImagePeer:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.prompts = []
        self.cancelled = []
        self.pending = {}
        self.sessions = 0
        self.mode = "ok"
        self.process = SimpleNamespace(
            stdin=SimpleNamespace(write=self.write, drain=AsyncMock()),
            stdout=self.stdout,
            stderr=asyncio.StreamReader(),
        )

    def send(self, value):
        self.stdout.feed_data(json.dumps({"jsonrpc": "2.0", **value}).encode() + b"\n")

    def write(self, frame):
        request = json.loads(frame)
        method = request["method"]
        params = request["params"]
        result = {}
        if method == "session/new":
            self.sessions += 1
            result = {
                "sessionId": f"session-{self.sessions}",
                "models": {"availableModels": [{"modelId": MODEL, "name": "Mock Vision Model"}]},
                "modes": {"availableModes": [{"id": "ask"}]},
            }
        elif method == "session/set_config_option":
            result = {"configOptions": [{"id": "model", "currentValue": MODEL}]}
        elif method == "session/prompt":
            self.prompts.append(params)
            session = params["sessionId"]
            if self.mode == "timeout":
                self.pending[session] = request["id"]
                return
            if self.mode in {"rpc_error", "rpc_unsupported"}:
                message = (
                    "Selected model does not support image inputs: PRIVATE_PAYLOAD"
                    if self.mode == "rpc_unsupported"
                    else "Invalid image: PRIVATE_PAYLOAD"
                )
                self.send(
                    {
                        "id": request["id"],
                        "error": {"code": -32603, "message": message},
                    }
                )
                return
            text = {
                "chunk_error": "Error: [invalid_argument] Invalid image: PRIVATE_PAYLOAD",
                "chunk_unsupported": "Error: [invalid_argument] Image inputs are not supported",
                "text_only": "This model only supports text.",
                "quoted": "Your previous model does not support images, but this shows a cat.",
            }.get(self.mode, "OK")
            self.send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )
            result = {"stopReason": "end_turn"}
        elif method == "session/cancel":
            session = params["sessionId"]
            self.cancelled.append(session)
            self.send({"id": self.pending.pop(session), "result": {"stopReason": "cancelled"}})
            return
        self.send({"id": request["id"], "result": result})


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.directory / "media"
        self.root.mkdir()
        self.first, self.second = self.root / "first.png", self.root / "second.png"
        make_card(self.first)
        make_card(self.second)
        self.peer = ImagePeer()
        self.client = ACPClient(
            self.peer.process, frame_limit=4096, output_limit=4096, ready={"cwd": "/remote/work"}
        )
        self.client.initialized = {"agentCapabilities": {"promptCapabilities": {"image": True}}}
        self.addAsyncCleanup(self.client.close)
        self.enterContext(
            patch(
                "astrbot.core.utils.astrbot_path.get_astrbot_temp_path", return_value=str(self.root)
            )
        )
        self.enterContext(patch.object(runtime, "_engine", None))
        self.enterContext(patch.object(runtime, "_settings", None))
        self.enterContext(patch.object(runtime, "_available_models", None))
        self.enterContext(patch.object(runtime, "_startup_failure", None))
        self.enterContext(patch.object(runtime, "_validation_stopped", False))
        runtime.configure({"model_id": MODEL, "image_unsupported_message": "这个模型看不了图喵。"})
        runtime._settings["model"] = MODEL
        runtime._available_models = [{"modelId": MODEL, "name": "Mock Vision Model"}]

    def provider(self):
        provider = CursorCLIProvider({"id": MODEL, "model": MODEL}, {})
        self.addAsyncCleanup(provider.terminate)
        provider.engine.client = self.client
        return provider

    async def test_image_only_local_file_and_mixed_multiple_images_use_inline_blocks(self):
        provider = self.provider()
        first = await provider.text_chat(image_urls=[self.first.as_uri()], conversation_id="first")
        self.assertEqual(first.completion_text, "OK")
        await provider.text_chat(
            prompt="answer these two",
            image_urls=[str(self.second)],
            conversation_id="second",
            extra_user_content_parts=[
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "base64://" + base64.b64encode(self.first.read_bytes()).decode()
                    },
                }
            ],
        )
        one, two = self.peer.prompts
        self.assertNotEqual(one["sessionId"], two["sessionId"])
        self.assertEqual([block["type"] for block in two["prompt"]], ["text", "image", "image"])
        self.assertIn('"images":[1,2]', two["prompt"][0]["text"])
        self.assertIn("answer these two", two["prompt"][0]["text"])
        self.assertNotIn(str(self.root), json.dumps(two))
        for block, path in zip(two["prompt"][1:], (self.first, self.second), strict=True):
            self.assertEqual(set(block), {"type", "mimeType", "data"})
            self.assertEqual(block["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(block["data"]), path.read_bytes())

    async def test_image_history_reuse_replay_and_changed_image(self):
        provider = self.provider()

        async def send(path):
            return await provider.text_chat(
                prompt="read", image_urls=[str(path)], conversation_id="same"
            )

        await send(self.first)
        await send(self.first)
        self.assertEqual(len(self.peer.prompts), 1)
        with self.first.open("rb") as file:
            ref = "data:image/png;base64," + base64.b64encode(file.read()).decode()
        await provider.text_chat(
            prompt="follow-up",
            contexts=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read"},
                        {"type": "image_url", "image_url": {"url": ref}},
                    ],
                },
                {"role": "assistant", "content": "OK"},
            ],
            conversation_id="same",
        )
        first, followup = self.peer.prompts
        self.assertEqual(first["sessionId"], followup["sessionId"])
        self.assertEqual(len(followup["prompt"]), 1)
        await send(self.second)
        self.assertNotEqual(first["sessionId"], self.peer.prompts[-1]["sessionId"])

    async def test_plain_text_without_image_capability_continues(self):
        provider = self.provider()
        self.assertEqual(provider.provider_config["modalities"], ["text", "image"])
        self.client.initialized["agentCapabilities"]["promptCapabilities"]["image"] = False
        self.assertEqual((await provider.text_chat(prompt="plain text")).completion_text, "OK")
        self.assertEqual(len(self.peer.prompts[-1]["prompt"]), 1)

    async def test_invalid_limits_and_path_boundaries(self):
        provider = self.provider()
        corrupt = self.root / "corrupt.png"
        corrupt.write_bytes(self.first.read_bytes()[:-40])
        too_large = self.root / "large.png"
        with too_large.open("wb") as file:
            file.truncate(images.MAX_IMAGE_BYTES + 1)
        outside = self.directory / "secret.png"
        make_card(outside)
        symlink = self.root / "symlink.png"
        symlink.symlink_to(outside)
        hardlink = self.root / "hardlink.png"
        os.link(outside, hardlink)
        for refs, expected in (
            ([str(corrupt)], "image_invalid"),
            ([str(too_large)], "image_limit"),
            ([str(self.first)] * (images.MAX_IMAGES + 1), "image_limit"),
            ([str(outside)], "image_source"),
            ([str(symlink)], "image_source"),
            ([str(hardlink)], "image_source"),
            (["https://example.invalid/x.png"], "image_source"),
        ):
            with self.assertRaises(CursorError) as error:
                await provider.text_chat(prompt="keep image", image_urls=refs)
            self.assertEqual(error.exception.code, expected)
            self.assertNotIn(str(self.directory), str(error.exception))
        with self.assertRaises(CursorError) as error:
            await provider.text_chat(
                prompt="keep image",
                extra_user_content_parts=[{"type": "text", "text": "[Image unavailable]"}],
            )
        self.assertEqual(error.exception.code, "image_source")
        self.assertFalse(self.peer.prompts)

    async def test_missing_capability_and_model_rejections_do_not_fall_back_to_text(self):
        provider = self.provider()
        self.client.initialized["agentCapabilities"]["promptCapabilities"]["image"] = False
        with self.assertRaises(CursorError) as error:
            await provider.text_chat(prompt="read", image_urls=[str(self.first)])
        self.assertEqual(error.exception.code, "image_unsupported")
        self.assertFalse(self.peer.prompts)
        self.client.initialized["agentCapabilities"]["promptCapabilities"]["image"] = True
        for mode in ("rpc_error", "chunk_error"):
            self.peer.mode = mode
            with self.assertRaises(CursorError) as error:
                await provider.text_chat(prompt="read", image_urls=[str(self.first)])
            self.assertEqual(error.exception.code, "image_unavailable")
            self.assertNotIn("PRIVATE_PAYLOAD", str(error.exception))
        self.assertEqual(len(self.peer.prompts), 2)

    async def test_model_unsupported_sends_the_custom_error_once_and_keeps_failure_code(self):
        from astrbot.core.platform.astr_message_event import current_message_event

        provider = self.provider()
        extra = {}
        event = SimpleNamespace(
            unified_msg_origin="mock-chat",
            get_extra=extra.get,
            set_extra=extra.__setitem__,
            send=AsyncMock(),
        )
        token = current_message_event.set(event)
        try:
            for mode in ("rpc_unsupported", "chunk_unsupported", "text_only"):
                before = len(self.peer.prompts)
                self.peer.mode = mode
                with self.assertRaises(CursorError) as error:
                    await provider.text_chat(prompt="read", image_urls=[str(self.first)])
                self.assertEqual(error.exception.code, "image_unsupported")
                self.assertIn("这个模型看不了图喵。", str(error.exception))
                self.assertNotIn("PRIVATE_PAYLOAD", str(error.exception))
                self.assertEqual(len(self.peer.prompts), before + 1)
        finally:
            current_message_event.reset(token)
        event.send.assert_awaited_once()
        self.assertEqual(event.send.await_args.args[0].chain[0].text, "这个模型看不了图喵。")

    async def test_model_reply_that_mentions_unsupported_images_stays_a_reply(self):
        provider = self.provider()
        self.peer.mode = "quoted"
        reply = await provider.text_chat(image_urls=[str(self.first)])
        self.assertIn("this shows a cat", reply.completion_text)

    async def test_image_types_and_payload_are_absent_from_plugin_logs(self):
        from PIL import Image

        provider = self.provider()
        refs = []
        for format in ("JPEG", "WEBP"):
            output = io.BytesIO()
            with Image.new("RGB", (16, 16), "red") as generated:
                generated.save(output, format=format)
            refs.append("base64://" + base64.b64encode(output.getvalue()).decode("ascii"))
        with self.assertLogs("astrbot", level="INFO") as logs:
            await provider.text_chat(prompt="read", image_urls=refs)
        blocks = self.peer.prompts[-1]["prompt"][1:]
        self.assertEqual([block["mimeType"] for block in blocks], ["image/jpeg", "image/webp"])
        for block in blocks:
            self.assertNotIn(block["data"], "\n".join(logs.output))

    async def test_timeout_cancels_image_turn_and_resets_that_session(self):
        provider = self.provider()
        provider.engine.cfg["timeout_seconds"] = 0.05
        self.peer.mode = "timeout"
        with self.assertRaises(CursorError) as error:
            await provider.text_chat(image_urls=[str(self.first)], conversation_id="timeout-test")
        self.assertEqual(error.exception.code, "timeout")
        self.assertEqual(self.peer.cancelled, [self.peer.prompts[0]["sessionId"]])
        self.peer.mode = "ok"
        response = await provider.text_chat(prompt="text again", conversation_id="timeout-test")
        self.assertEqual(response.completion_text, "OK")
        self.assertNotEqual(self.peer.prompts[0]["sessionId"], self.peer.prompts[-1]["sessionId"])
