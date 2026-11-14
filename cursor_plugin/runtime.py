"""Plugin configuration only; never patch AstrBot or another provider."""

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


def suspend():
    global _settings
    _settings = None
