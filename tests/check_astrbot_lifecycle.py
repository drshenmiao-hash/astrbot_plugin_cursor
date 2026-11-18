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
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests.mock_runtime import MockOwner

PLUGIN = "astrbot_plugin_cursor"
MODEL = "mock-plan-model"
PREFIX = f"data.plugins.{PLUGIN}.cursor_plugin"
ROOT = Path(__file__).resolve().parents[1]


async def run_chat(provider, prompt, *, max_attempts=1):
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
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
        request=ProviderRequest(prompt=prompt),
        run_context=ContextWrapper(context=None),
        tool_executor=MagicMock(),
        agent_hooks=BaseAgentRunHooks(),
        fallback_providers=[backup],
        request_max_retries=max_attempts,
    )
    replies = [
        response.completion_text async for response in runner._iter_llm_responses_with_fallback()
    ]
    return replies, backup.text_chat.await_count


async def verify(root):
    from astrbot.core import LogBroker, db_helper
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

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
    finally:
        await host.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cursor-astrbot-lifecycle-") as directory:
        os.environ["ASTRBOT_ROOT"] = directory
        sys.path.insert(0, directory)
        asyncio.run(verify(Path(directory)))
