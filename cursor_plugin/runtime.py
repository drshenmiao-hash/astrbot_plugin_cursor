# SPDX-License-Identifier: AGPL-3.0-only
# Provider reload logic adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""Plugin configuration and native Provider lifecycle integration."""

from .engine import Engine
from .errors import CursorError
from .settings import settings_from

PROVIDER_TYPE = "cursor_cli"
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


async def initialize(context):
    """Replace retained model instances using the reference plugin's pattern."""
    from .provider import CursorCLIProvider

    manager = getattr(context, "provider_manager", None)
    if manager is None:
        return
    config = context.astrbot_config_mgr.default_conf
    source_ids = {
        source["id"]
        for source in config.get("provider_sources", [])
        if source.get("type") == PROVIDER_TYPE
    }
    instances = getattr(manager, "inst_map", {})
    selected = getattr(manager, "curr_provider_inst", None)
    selected_id = next((key for key, instance in instances.items() if instance is selected), None)
    try:
        for model in list(config.get("provider", [])):
            if model["id"] not in instances or not (
                model.get("provider_source_id") in source_ids or model.get("type") == PROVIDER_TYPE
            ):
                continue
            await manager.reload(model)
            if model.get("enable", True) and not isinstance(
                instances.get(model["id"]), CursorCLIProvider
            ):
                raise CursorError("config")
    finally:
        if selected_id in instances:
            manager.curr_provider_inst = instances[selected_id]
