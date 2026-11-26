"""Exercise real AstrBot reload and fallback with an offline ACP subprocess.

Run with: python -m tests.check_astrbot_lifecycle
"""

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.mock_runtime import MockOwner

PLUGIN = "astrbot_plugin_cursor"
MODEL = "mock-plan-model"
PREFIX = f"data.plugins.{PLUGIN}.cursor_plugin"
ROOT = Path(__file__).resolve().parents[1]


async def run_chat(
    provider,
    prompt,
    *,
    max_attempts=1,
    cid=None,
    history=None,
    system=None,
    image_urls=None,
    extra_parts=None,
    saved=None,
):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.db.po import Conversation
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
    from astrbot.core.provider.provider import Provider

    backup = MagicMock(spec=Provider)
    backup.provider_config = {"id": "mock-backup", "modalities": ["text"]}
    backup.get_model.return_value = "mock-backup-model"
    backup.text_chat = AsyncMock(
        return_value=LLMResponse(role="assistant", completion_text="BACKUP")
    )
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=ProviderRequest(
            prompt=prompt,
            conversation=Conversation("mock-platform", "mock-user", cid) if cid else None,
            contexts=history or [],
            system_prompt=system,
            image_urls=image_urls or [],
            extra_user_content_parts=extra_parts or [],
        ),
        run_context=ContextWrapper(context=None),
        tool_executor=MagicMock(),
        agent_hooks=BaseAgentRunHooks(),
        fallback_providers=[backup],
        request_max_retries=max_attempts,
    )
    replies = [
        response.completion_text async for response in runner._iter_llm_responses_with_fallback()
    ]
    if saved is not None:
        messages = []
        for message in runner.run_context.messages:
            if message.role == "system" or getattr(message, "_no_save", False):
                continue
            dumped = message.model_dump()
            if isinstance(message.content, list):
                dumped["content"] = [
                    part.model_dump()
                    for part in message.content
                    if not getattr(part, "_no_save", False)
                ]
            messages.append(dumped)
        saved[:] = [*messages, {"role": "assistant", "content": replies[-1]}]
    return replies, backup.text_chat.await_count


async def verify_sessions(provider):
    original = [{"role": "user", "content": "第一个问题"}, {"role": "assistant", "content": "OK"}]
    assert await run_chat(provider, "第一个问题", cid="chat-a", system="原人格") == (["OK"], 0)
    cache = provider.engine.sessions
    key = next(iter(cache))
    first_session = cache[key]["session_id"]

    await provider.text_chat(prompt="后台总结", system_prompt="总结任务")
    assert cache[key]["session_id"] == first_session and len(cache) == 1
    assert await run_chat(
        provider, "第二个问题", cid="chat-a", history=original, system="新人格"
    ) == (["OK"], 0)
    assert cache[key]["session_id"] == first_session
    assert cache[key]["history"]["systems"][0]["content"] == "新人格"

    assert await run_chat(provider, "另一段对话", cid="chat-b") == (["OK"], 0)
    assert len(cache) == 2
    assert len({entry["session_id"] for entry in cache.values()}) == 2
    revised = [{"role": "user", "content": "历史被改写"}, original[1]]
    assert await run_chat(provider, "继续", cid="chat-a", history=revised, system="新人格") == (
        ["OK"],
        0,
    )
    assert cache[key]["session_id"] != first_session
    print(
        "PASS native conversation_id enables reuse; persona updates incrementally; different chats and unscoped work stay separate; history edits rebuild"
    )


async def verify(root):
    from astrbot.core import LogBroker, db_helper
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.provider.entities import ProviderType
    from astrbot.dashboard.services.config_service import (
        ConfigProfileService,
        ProviderConfigService,
    )

    installed = root / "data/plugins" / PLUGIN
    shutil.copytree(
        ROOT, installed, ignore=shutil.ignore_patterns(".git", ".ruff_cache", "__pycache__")
    )
    config_path = root / "data/config" / f"{PLUGIN}_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {"model_id": MODEL, "transport": "local", "context_max_bytes": 1024}
    config_path.write_text(json.dumps(config))

    runtime = importlib.import_module(PREFIX + ".runtime")
    runtime.Engine = partial(runtime.Engine, owner_factory=MockOwner)
    host = AstrBotCoreLifecycle(LogBroker(), db_helper)
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", new_callable=AsyncMock):
        await host.initialize()

    manager = host.provider_manager
    plugins = host.plugin_manager
    original_import = plugins._import_plugin_with_dependency_recovery

    async def import_with_mock_owner(**kwargs):
        module = await original_import(**kwargs)
        if kwargs.get("root_dir_name") == PLUGIN and module.runtime is not runtime:
            module.runtime.Engine = partial(module.runtime.Engine, owner_factory=MockOwner)
        return module

    async def wait_for_prompt(provider):
        async with asyncio.timeout(3):
            while not any(turn.get("wire_bytes") for turn in provider.engine.client.turns.values()):
                await asyncio.sleep(0.01)

    try:
        system_config = host.astrbot_config_mgr.default_conf
        cursor_model = next(model for model in system_config["provider"] if model["id"] == MODEL)
        cursor_source = next(
            source
            for source in system_config["provider_sources"]
            if source["id"] == cursor_model["provider_source_id"]
        )
        assert cursor_source["key"] == "" and cursor_model["key"] == []
        profiles = ConfigProfileService(host, runtime={})
        with patch.object(host, "reload_pipeline_scheduler", new_callable=AsyncMock) as reload:
            cursor_model["key"] = ""
            try:
                await profiles.update_profile("default", dict(system_config))
            except ValueError as exc:
                assert "provider.key" in str(exc), exc
            else:
                raise AssertionError("AstrBot accepted the old string model key")
            finally:
                cursor_model["key"] = []
            assert await profiles.update_profile("default", dict(system_config)) == "保存成功~"
            reload.assert_awaited_once_with("default")
        persisted = json.loads((root / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
        assert next(model for model in persisted["provider"] if model["id"] == MODEL)["key"] == []
        assert (
            next(
                source for source in persisted["provider_sources"] if source["type"] == "cursor_cli"
            )["key"]
            == ""
        )
        print(
            "PASS official AstrBot system config save rejects the old key string and persists the fixed types"
        )

        first = manager.inst_map[MODEL]
        assert await run_chat(first, "普通回复") == (["OK"], 0)
        assert await run_chat(first, "MOCK_STREAM_FAILURE") == (["BACKUP"], 1)
        codes = runtime._usage_store.summary()["rows"][0]["codes"]
        assert codes == {"ok": 1, "ping_timeout": 1}, codes
        print(
            "PASS single maximum attempt and terminal ACP error without a newline fall back, with one failed usage record"
        )

        assert await run_chat(first, "x" * 2048) == (["BACKUP"], 1)
        usage = await runtime.usage_dashboard(7)
        assert usage["totals"]["failed"] == 2
        assert any(row["label"] == "context_limit" for row in usage["groups"]["code"])
        print("PASS context-limit rejection counts as a local failure before sending ACP")

        caller = asyncio.create_task(run_chat(first, "CANCEL_TEST caller"))
        await wait_for_prompt(first)
        caller.cancel()
        (cancelled,) = await asyncio.gather(caller, return_exceptions=True)
        assert isinstance(cancelled, asyncio.CancelledError)
        async with asyncio.timeout(3):
            while first.engine.active:
                await asyncio.sleep(0.01)
        print("PASS caller cancellation remains cancellation")

        inflight = asyncio.create_task(run_chat(first, "CANCEL_TEST reload"))
        await wait_for_prompt(first)
        old_process = first.engine.owner.process
        with patch.object(
            plugins, "_import_plugin_with_dependency_recovery", side_effect=import_with_mock_owner
        ):
            success, error = await plugins.reload(PLUGIN)
        assert success, error
        assert await asyncio.wait_for(inflight, 5) == (["BACKUP"], 1)
        assert old_process.returncode is not None
        current = manager.inst_map[MODEL]
        assert current is not first and await run_chat(current, "新实例") == (["OK"], 0)
        print(
            "PASS successful hot reload stops the old process, falls back for in-flight chat and serves later chat"
        )

        old_process = current.engine.owner.process
        config_path.write_text(json.dumps({**config, "cli_binary": str(root / "missing-cli")}))
        success, error = await plugins.reload(PLUGIN)
        assert success, error
        cleared = importlib.import_module(PREFIX + ".runtime")
        report = await cleared.usage_dashboard(7)
        assert report["startup_error"]["code"] == "cli_missing"
        assert old_process.returncode is not None
        assert await run_chat(manager.inst_map[MODEL], "启动失败") == (["BACKUP"], 1)
        print(
            "PASS failed hot reload preserves usage and falls back instead of invoking the old process"
        )

        config_path.write_text(json.dumps(config))
        with patch.object(
            plugins, "_import_plugin_with_dependency_recovery", side_effect=import_with_mock_owner
        ):
            success, error = await plugins.reload(PLUGIN)
        assert success, error
        current = manager.inst_map[MODEL]
        assert await run_chat(current, "恢复配置") == (["OK"], 0)
        old_process = current.engine.owner.process
        await plugins.turn_off_plugin(PLUGIN)
        assert not host.star_context.get_registered_star(PLUGIN).activated
        assert old_process.returncode is not None
        assert await run_chat(manager.inst_map[MODEL], "已禁用") == (["BACKUP"], 1)
        with patch.object(
            plugins, "_import_plugin_with_dependency_recovery", side_effect=import_with_mock_owner
        ):
            await plugins.turn_on_plugin(PLUGIN)
        assert await run_chat(manager.inst_map[MODEL], "重新启用") == (["OK"], 0)
        print("PASS disabling stops the provider process; enabling restores normal chat")

        original_instance = manager.inst_map[MODEL]
        old_process = original_instance.engine.owner.process
        config_keys = ("agent", "provider_settings")
        selection = {key: deepcopy(host.astrbot_config.get(key)) for key in config_keys}
        probes = []

        async def import_with_busy_runner(**kwargs):
            module = await import_with_mock_owner(**kwargs)
            if kwargs.get("root_dir_name") == PLUGIN:
                recovered = module.runtime
                recovered._STARTUP_BACKOFF = (0.01, 0.02)
                real_probe = recovered._fetch_available_models

                async def probe():
                    assert old_process.returncode is not None
                    probes.append(None)
                    if len(probes) <= 3:
                        raise module.CursorError("runner_in_use")
                    await asyncio.sleep(0.02)
                    return await real_probe()

                recovered._fetch_available_models = probe
            return module

        with patch.object(
            plugins, "_import_plugin_with_dependency_recovery", side_effect=import_with_busy_runner
        ):
            success, error = await plugins.reload(PLUGIN)
        assert success, error
        pending_provider = manager.inst_map[MODEL]
        assert pending_provider is not original_instance and pending_provider.engine is None
        recovering_runtime = importlib.import_module(PREFIX + ".runtime")
        assert (await recovering_runtime.usage_dashboard(7))["startup_error"][
            "code"
        ] == "runner_in_use"
        assert len(probes) == 3

        from astrbot.core.platform.astr_message_event import current_message_event

        extras = {}
        event = SimpleNamespace(
            unified_msg_origin="mock-origin",
            get_extra=extras.get,
            set_extra=extras.__setitem__,
            send=AsyncMock(),
        )
        before = recovering_runtime._usage_store.summary()["rows"][0]
        token = current_message_event.set(event)
        try:
            assert await run_chat(pending_provider, "启动忙", cid="busy-chat") == (["BACKUP"], 1)
        finally:
            current_message_event.reset(token)
        event.send.assert_awaited_once()
        notice = event.send.await_args.args[0].chain[0].text
        assert "runner_in_use" in notice and "mock-backup" not in notice
        after = recovering_runtime._usage_store.summary()["rows"][0]
        assert after["requests"] - before["requests"] == 1
        assert after["success"] == before["success"]
        assert after["codes"].get("runner_in_use", 0) - before["codes"].get("runner_in_use", 0) == 1
        assert len(probes) == 3
        print(
            "PASS startup busy is capped; chat fallback emits a factual notice and counts the rejected request once"
        )

        recovering_runtime._retry_validation_after = 0
        responses = await asyncio.gather(
            *(run_chat(pending_provider, f"恢复测试 {i}") for i in range(4))
        )
        assert responses == [(["OK"], 0)] * 4 and len(probes) == 4
        assert (await recovering_runtime.usage_dashboard(7))["startup_error"] is None
        assert {key: host.astrbot_config.get(key) for key in config_keys} == selection
        print(
            "PASS concurrent chats share one real simulated ACP catalog revalidation and recover without a reload"
        )
        await verify_sessions(pending_provider)
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
        from PIL import Image

        image_path = Path(get_astrbot_temp_path()) / "host-fixture.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGB", (32, 32), "red") as fixture:
            fixture.save(image_path)
        assert pending_provider.provider_config["modalities"] == ["text", "image"]
        assert await run_chat(
            pending_provider, "MOCK_IMAGE_REQUIRED", image_urls=[str(image_path)]
        ) == (["INLINE-IMAGE"], 0)
        print(
            "PASS native AstrBot ProviderRequest carried a prepared image inline to the selected mock ACP model"
        )

        from astrbot.core.agent.message import TextPart

        saved = []
        initial_keys = set(pending_provider.engine.sessions)
        for index, with_image in enumerate((True, False, False, False, True)):
            extra = TextPart(text=f"One-turn context {index}").mark_as_temp()
            label = "MOCK_IMAGE_REQUIRED" if with_image else "MOCK_NO_IMAGE"
            assert await run_chat(
                pending_provider,
                f"{label} {index}",
                cid="image-sequence",
                history=saved,
                image_urls=[str(image_path)] if with_image else [],
                extra_parts=[extra],
                saved=saved,
            ) == (["INLINE-IMAGE" if with_image else "NO-IMAGE"], 0)
            (key,) = pending_provider.engine.sessions.keys() - initial_keys
            current_session = pending_provider.engine.sessions[key]["session_id"]
            if index == 0:
                original_session = current_session
            else:
                assert current_session == original_session
        print(
            "PASS actual AstrBot payload image/text/text/text/image reuses one ACP session and sends images only on the image turns"
        )

        other_id = "dashboard-cursor-model"
        other_model = "mock-second-model"
        catalog = [{"modelId": model} for model in (MODEL, other_model)]
        with patch.dict(os.environ, {"TEST_CURSOR_CATALOG": json.dumps(catalog)}):
            with patch.object(
                plugins,
                "_import_plugin_with_dependency_recovery",
                side_effect=import_with_mock_owner,
            ):
                success, error = await plugins.reload(PLUGIN)
            assert success, error
            await ProviderConfigService(host).create_provider(
                {**cursor_model, "id": other_id, "model": other_model}, cursor_source["id"]
            )
            for chosen, expected in ((MODEL, MODEL), (other_id, other_model), (MODEL, MODEL)):
                updated = deepcopy(dict(host.astrbot_config_mgr.default_conf))
                updated["agent_runner"]["config"]["model"]["provider_id"] = chosen
                with patch.object(host, "reload_pipeline_scheduler", new_callable=AsyncMock):
                    assert await profiles.update_profile("default", updated) == "保存成功~"
                provider = await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)
                assert provider.meta().id == chosen
                replies, fallbacks = await run_chat(provider, "MOCK_MODEL_ID", cid="model-switch")
                assert fallbacks == 0 and replies[0].startswith(expected + "/"), replies
            assert json.loads(config_path.read_text(encoding="utf-8-sig"))["model_id"] == MODEL
        print(
            "PASS real AstrBot model registration and system selection switch the ACP target without editing the plugin seed or reloading between selections"
        )
    finally:
        await host.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cursor-astrbot-lifecycle-") as directory:
        os.environ["ASTRBOT_ROOT"] = directory
        sys.path.insert(0, directory)
        asyncio.run(verify(Path(directory)))
