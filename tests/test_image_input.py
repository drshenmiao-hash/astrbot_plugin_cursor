"""Native Provider through ACP framing, with an in-memory peer and real images."""

import asyncio
import base64
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cursor_plugin import images, metrics, runtime
from cursor_plugin.acp import ACPClient
from cursor_plugin.errors import CursorError
from cursor_plugin.provider import CursorCLIProvider
from cursor_plugin.usage import UsageStore
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

    @staticmethod
    def image_part(path):
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}

    async def test_image_then_text_then_image_reuses_sessions_with_assembled_extra_parts(self):
        from PIL import Image

        provider = self.provider()
        large = self.root / "larger.png"
        with Image.frombytes("RGB", (400, 400), random.Random(4).randbytes(480_000)) as fixture:
            fixture.save(large)
        history = []
        with self.assertLogs("astrbot", level="INFO") as logs:
            for turn, media in enumerate((large, None, None, None, self.second)):
                temporary = {"type": "text", "text": f"Fixture reminder {turn}"}
                parts = [{"type": "text", "text": f"Current message {turn}"}]
                if media:
                    parts.append(self.image_part(media))
                # AstrBot has serialized the same part into its current user entry and passes
                # the original, still marked transient, through the separate argument as well.
                incoming = {"role": "user", "content": [*parts[:1], temporary, *parts[1:]]}
                reply = await provider.text_chat(
                    contexts=[*history, incoming],
                    conversation_id="sequence",
                    extra_user_content_parts=[{**temporary, "_no_save": True}],
                )
                self.assertEqual(reply.completion_text, "OK")
                history.extend(
                    [{"role": "user", "content": parts}, {"role": "assistant", "content": "OK"}]
                )
        self.assertEqual(len({turn["sessionId"] for turn in self.peer.prompts}), 1)
        self.assertEqual([len(turn["prompt"]) - 1 for turn in self.peer.prompts], [1, 0, 0, 0, 1])
        for turn, prompt in enumerate(self.peer.prompts):
            self.assertEqual(prompt["prompt"][0]["text"].count(f"Fixture reminder {turn}"), 1)
            self.assertNotIn("_history_content", prompt["prompt"][0]["text"])
        finished = [
            json.loads(line.split("cursor_provider ", 1)[1])
            for line in logs.output
            if '"event":"request_finished"' in line
        ]
        self.assertEqual([row["reused"] for row in finished], [False, True, True, True, True])
        self.assertGreater(finished[0]["wire_bytes"], 600_000)
        self.assertTrue(all(row["wire_bytes"] < 1024 for row in finished[1:4]))
        print("image/text regression wire_bytes:", [row["wire_bytes"] for row in finished])

    async def test_host_prepared_image_extras_are_not_sent_twice(self):
        from astrbot.core.agent.message import ImageURLPart, Message
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.provider.modalities import sanitize_contexts_by_modalities

        provider = self.provider()
        third = self.root / "third.png"
        make_card(third)
        with self.subTest("tool loop with three assembled images"):
            extras = [
                ImageURLPart(image_url=ImageURLPart.ImageURL(url=str(path)))
                for path in (self.first, self.second, third)
            ]
            request = ProviderRequest(prompt="Three images", extra_user_content_parts=extras)
            current = Message.model_validate(await request.assemble_context())
            contexts, _ = sanitize_contexts_by_modalities([current], ["text", "image"])
            # The original path may already have been removed by AstrBot.
            third.unlink()
            self.assertEqual(
                (
                    await provider.text_chat(
                        contexts=contexts,
                        extra_user_content_parts=extras,
                        abort_signal=asyncio.Event(),
                    )
                ).completion_text,
                "OK",
            )
            self.assertEqual(len(self.peer.prompts[-1]["prompt"]) - 1, 3)
        with self.subTest("direct call accepts a genuinely unsent image extra"):
            self.assertEqual(
                (
                    await provider.text_chat(
                        contexts=[{"role": "user", "content": "One image"}],
                        extra_user_content_parts=extras[:1],
                    )
                ).completion_text,
                "OK",
            )
            self.assertEqual(len(self.peer.prompts[-1]["prompt"]) - 1, 1)

    async def test_reset_preserves_history_text_without_reading_or_counting_old_images(self):
        provider = self.provider()
        history = []
        for index in range(images.MAX_IMAGES + 2):
            history.extend(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Earlier message {index}"},
                            self.image_part(self.first)
                            if index <= images.MAX_IMAGES
                            else {
                                "type": "image_url",
                                "image_url": {"url": f"/missing/old-image-{index}.png"},
                            },
                        ],
                    },
                    {"role": "assistant", "content": "Earlier caption"},
                ]
            )
        for refs, count in (([], 0), ([str(self.first)], 1)):
            await provider.text_chat(
                prompt="continue after reset", contexts=history, image_urls=refs
            )
            request = self.peer.prompts[-1]["prompt"]
            self.assertEqual(len(request) - 1, count)
            self.assertEqual(
                request[0]["text"].count('"historical_images_not_attached":1'),
                images.MAX_IMAGES + 2,
            )
            self.assertIn("Earlier caption", request[0]["text"])
            self.assertNotIn("/missing", request[0]["text"])

    async def test_limit_log_shows_current_count_bytes_and_pixels(self):
        provider = self.provider()
        store = UsageStore(self.directory / "usage")
        self.addCleanup(store.close)
        self.enterContext(patch.object(metrics, "_usage_store", store))
        path = str(self.first)
        cases = (
            ("count", "MAX_IMAGES", 1, [path, path]),
            ("file_bytes", "MAX_IMAGE_BYTES", 1, [path]),
            ("total_bytes", "MAX_IMAGE_TOTAL_BYTES", self.first.stat().st_size, [path, path]),
            ("pixels", "MAX_IMAGE_PIXELS", 100, [path]),
        )
        for kind, constant, maximum, refs in cases:
            with self.subTest(kind=kind):
                with patch.object(images, constant, maximum), self.assertLogs("astrbot") as logs:
                    with self.assertRaises(CursorError) as error:
                        await provider.text_chat(prompt="limit", image_urls=refs)
                self.assertEqual(error.exception.code, "image_limit")
                row = next(
                    json.loads(line.split("cursor_provider ", 1)[1])
                    for line in logs.output
                    if '"event":"request_finished"' in line
                )
                self.assertEqual(row["image_limit_kind"], kind)
                self.assertEqual(row["image_limit_max"], maximum)
                self.assertGreater(row["image_limit_actual"], maximum)
                dashboard = await asyncio.to_thread(store.dashboard, 7)
                recent = dashboard["recent_request"]
                self.assertEqual(recent["outcome"], "图片预检拒绝，Cursor 请求未发送")
                self.assertEqual((recent["stage"], recent["delivery"]), ("preflight", "not_sent"))
                self.assertIsNone(recent["model_id"])
        self.assertEqual(dashboard["totals"]["requests"], len(cases))
        self.assertEqual(self.peer.prompts, [])

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
