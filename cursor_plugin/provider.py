# SPDX-License-Identifier: AGPL-3.0-only
# Adapter registration adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""A native AstrBot chat provider backed exclusively by official Cursor CLI."""

import asyncio

from astrbot.api.provider import LLMResponse, Provider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter

from . import runtime
from .errors import CursorError
from .protocol import explicit_model
from .transcript import build_transcript
from .transport import SSHTransport

TEMPLATE = {
    "id": "cursor-cli",
    "provider": "cursor",
    "type": runtime.PROVIDER_TYPE,
    "provider_type": "chat_completion",
    "enable": True,
    "key": [],
    "api_base": "",
}


def register_adapter(cls):
    metadata = provider_cls_map.get(runtime.PROVIDER_TYPE)
    if metadata is None:
        register_provider_adapter(
            runtime.PROVIDER_TYPE,
            "Cursor 官方 CLI（纯文本 / Ask）",
            default_config_tmpl=dict(TEMPLATE),
            provider_display_name="Cursor CLI",
        )(cls)
    elif not getattr(metadata.cls_type, "_astrbot_cursor_plugin", False):
        raise CursorError("config")
    # AstrBot keeps the registry across module reloads. Publish the new class
    # every time, as in the reference plugin, without registering twice.
    metadata = provider_cls_map[runtime.PROVIDER_TYPE]
    metadata.cls_type = cls
    metadata.default_config_tmpl = dict(TEMPLATE)
    return cls


@register_adapter
class CursorCLIProvider(Provider):
    _astrbot_cursor_plugin = True

    def __init__(self, provider_config, provider_settings):
        # Dashboard-created models can default to image/audio/tool support.
        # Declare this adapter's actual capabilities on its own config copy.
        config = {**provider_config, "modalities": ["text"]}
        super().__init__(config, provider_settings)
        cfg = runtime.settings()
        self.set_model(provider_config.get("model") or cfg["model"])
        self.transport = SSHTransport(cfg)
        self._pending = set()
        self._closed = False

    def get_current_key(self):
        return ""

    def get_keys(self):
        return []

    def set_key(self, key):
        # The native source's Key field is deliberately unused. Never store it.
        if key:
            raise CursorError("config")

    async def get_models(self):
        # No CLI invocation or billing request just to open the model picker.
        model = runtime.settings()["model"]
        return [model] if model else []

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice="auto",
        request_max_retries=None,
        **kwargs,
    ):
        try:
            return await self._complete_text(
                prompt=prompt,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                tool_choice=tool_choice,
            )
        except asyncio.CancelledError:
            raise
        except CursorError:
            raise
        except Exception:
            # Also sanitize failures while serializing framework content objects.
            raise CursorError("cli_failed") from None

    async def _complete_text(
        self,
        *,
        prompt,
        image_urls,
        audio_urls,
        func_tool,
        contexts,
        system_prompt,
        tool_calls_result,
        model,
        extra_user_content_parts,
        tool_choice,
    ):
        if self._closed:
            raise CursorError("closed")
        has_tools = func_tool is not None and (
            not func_tool.empty()
            if callable(getattr(func_tool, "empty", None))
            else bool(func_tool)
        )
        if image_urls or audio_urls or has_tools or tool_calls_result or tool_choice == "required":
            raise CursorError("unsupported")
        chosen = explicit_model(model if model is not None else self.get_model())
        if chosen != self.transport.settings["model"]:
            raise CursorError("model")
        transcript = build_transcript(prompt, contexts, system_prompt, extra_user_content_parts)
        task = asyncio.create_task(self.transport.complete(chosen, transcript))
        self._pending.add(task)
        try:
            text = await task
            return LLMResponse(role="assistant", completion_text=text)
        finally:
            self._pending.discard(task)

    async def text_chat_stream(self, *args, **kwargs):
        # AstrBot may request streaming. Emit one final response, never fake deltas.
        yield await self.text_chat(*args, **kwargs)

    async def terminate(self):
        self._closed = True
        tasks = list(self._pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
