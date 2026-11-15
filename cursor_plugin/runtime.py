# SPDX-License-Identifier: AGPL-3.0-only
# Source persistence and reload adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""Plugin configuration and native Provider lifecycle integration."""

import logging
from copy import deepcopy

from .engine import Engine
from .errors import CursorError
from .settings import settings_from

PROVIDER_TYPE = "cursor_cli"
DEFAULT_SOURCE_ID = PROVIDER_TYPE
logger = logging.getLogger("astrbot")
_settings = None
_engine = None


def configure(config):
    global _settings, _engine
    _settings = settings_from(config)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        _settings["protected_paths"].append(get_astrbot_data_path())
    except ImportError:
        pass
    if _engine is not None and _engine.cfg != _settings:
        # Retained providers release their old engine during manager.reload().
        # New instances must not silently keep the previous configuration when
        # several native model instances are reloaded one after another.
        _engine = None


def settings():
    if _settings is None:
        raise CursorError("closed")
    return dict(_settings)


def acquire_engine():
    global _engine
    if _engine is None or _engine.closed:
        _engine = Engine(settings())
    _engine.references += 1
    return _engine


async def release_engine(engine):
    global _engine
    engine.references -= 1
    if engine.references == 0:
        await engine.close()
        if _engine is engine:
            _engine = None


def engine_health():
    return (
        _engine.health()
        if _engine is not None
        else {"process_alive": False, "active": 0, "queue_depth": 0, "cached_sessions": 0}
    )


def _build_default_source_config():
    return {
        "id": DEFAULT_SOURCE_ID,
        "provider": "cursor",
        "type": PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "enable": True,
        "key": "",
        "api_base": "",
        "modalities": ["text"],
    }


async def _commit_configuration(config, updates):
    """Reference ProviderRegistry._commit_configuration, without account storage."""
    missing = object()
    changes = {key: value for key, value in updates.items() if config.get(key, missing) != value}
    if not changes:
        return
    previous = {key: deepcopy(config[key]) if key in config else missing for key in changes}
    proposed = deepcopy(changes)
    config.update(changes)
    try:
        if await config.save_config_async() is False and await config.save_config_async() is False:
            raise CursorError("registration_save_failed")
    except BaseException as exc:
        for key, value in previous.items():
            # As in the reference plugin, do not overwrite a concurrent edit.
            if config.get(key) != proposed[key]:
                continue
            if value is missing:
                config.pop(key, None)
            else:
                config[key] = value
        if not isinstance(exc, Exception):
            raise
        raise CursorError("registration_save_failed") from None


async def ensure_default_source_and_model(config):
    """Publish only our source/model; never change model selection or fallbacks."""
    sources = deepcopy(config.get("provider_sources", []))
    models = deepcopy(config.get("provider", []))
    owned_sources = [s for s in sources if s.get("type") == PROVIDER_TYPE]
    source = next((s for s in owned_sources if s["id"] == DEFAULT_SOURCE_ID), None)
    source = source or next(iter(owned_sources), None)
    if source is None:
        if any(s.get("id") == DEFAULT_SOURCE_ID for s in sources):
            raise CursorError("source_id_conflict")
        source = _build_default_source_config()
        sources.append(source)
        owned_sources.append(source)
    for item in owned_sources:
        item.update(key="", api_base="", provider_type="chat_completion", modalities=["text"])
        item.setdefault("enable", True)
    source_ids = {s["id"] for s in owned_sources}

    def owned(model):
        sid = model.get("provider_source_id")
        return sid in source_ids or (not sid and model.get("type") == PROVIDER_TYPE)

    for item in models:
        if owned(item):
            item.update(key="", api_base="", modalities=["text"])
    model_id = settings()["model"]
    target = None
    if not model_id:
        logger.warning(
            "Cursor model_id 为空或设置为 auto/default，未创建原生模型；"
            "请填写明确的套餐内模型 ID 后重载插件。不会自动选模型。"
        )
    else:
        target = next((m for m in models if owned(m) and m.get("id") == model_id), None)
        target = target or next(
            (m for m in models if owned(m) and m.get("model") == model_id), None
        )
        if target is None and any(m.get("id") == model_id for m in models):
            logger.warning(
                "Cursor 原生模型 ID 已被其他提供商占用，未创建或覆盖模型；"
                "请先处理 ID 冲突。不会自动改名或选模型。"
            )
        else:
            if target is None:
                target = {"id": model_id, "enable": source.get("enable", True)}
                models.append(target)
            elif target["id"] != model_id:
                logger.info(
                    "Cursor 已存在相同模型，沿用其原生 ID，不重复创建或重命名；"
                    "保留默认模型与备用模型引用。"
                )
            target.update(
                model=model_id,
                provider_source_id=source["id"],
                type=PROVIDER_TYPE,
                provider_type="chat_completion",
                key="",
                api_base="",
                modalities=["text"],
            )
            target.setdefault("enable", True)
    await _commit_configuration(config, {"provider_sources": sources, "provider": models})
    return target["id"] if target else None


async def initialize(context):
    """Ensure native config, then refresh instances with the OAuth plugin's lifecycle."""
    config = context.astrbot_config_mgr.default_conf
    target_id = await ensure_default_source_and_model(config)
    manager = getattr(context, "provider_manager", None)
    if manager is None:
        return
    # Same on_change synchronization as the reference plugin's coordinator.
    manager.provider_sources_config = config.get("provider_sources", [])
    manager.providers_config = config.get("provider", [])
    source_ids = {
        source["id"]
        for source in config.get("provider_sources", [])
        if source.get("type") == PROVIDER_TYPE
    }
    instances = getattr(manager, "inst_map", {})
    selected = getattr(manager, "curr_provider_inst", None)
    selected_id = next((key for key, instance in instances.items() if instance is selected), None)
    # On cold start AstrBot loads providers after plugin.initialize(). Its MCP
    # initialization task also distinguishes a running manager with zero models.
    running = bool(instances) or getattr(manager, "_mcp_init_task", None) is not None
    try:
        for model in list(config.get("provider", [])):
            own_model = model.get("provider_source_id") in source_ids or (
                not model.get("provider_source_id") and model.get("type") == PROVIDER_TYPE
            )
            should_load = model["id"] in instances or (
                running and model["id"] == target_id and model.get("enable", True)
            )
            if not own_model or not should_load:
                continue
            from .provider import CursorCLIProvider

            await manager.reload(model)
            if model.get("enable", True) and not isinstance(
                instances.get(model["id"]), CursorCLIProvider
            ):
                raise CursorError("config")
    finally:
        # Preserve None too: host reload() may otherwise select the new model.
        manager.curr_provider_inst = (
            instances.get(selected_id, selected) if selected_id else selected
        )
