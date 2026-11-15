# SPDX-License-Identifier: AGPL-3.0-only
# Provider reload logic adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""Plugin configuration and native Provider lifecycle integration."""

from .errors import CursorError
from .transport import settings_from

PROVIDER_TYPE = "cursor_cli"
_settings = None


def configure(config):
    global _settings
    _settings = settings_from(config)


def settings():
    if _settings is None:
        raise CursorError("closed")
    return dict(_settings)


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
