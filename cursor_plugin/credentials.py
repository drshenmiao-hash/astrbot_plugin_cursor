"""Browser-login files and a minimal CLI environment; never log their contents."""

import json
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from .errors import CursorError
from .protocol import encode

DENY = ["Shell(*)", "Read(**)", "Read(/**)", "Write(**)", "Write(/**)", "WebFetch(*)", "Mcp(*:*)"]


def cli_env(home):
    home = str(home)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "NO_OPEN_BROWSER": "1",
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_CACHE_HOME": home + "/.cache",
        "XDG_DATA_HOME": home + "/.local/share",
        "CURSOR_CONFIG_DIR": home + "/.config/cursor",
        "CURSOR_DATA_DIR": home + "/.cursor",
        "AGENT_CLI_CREDENTIAL_STORE": "file",
    }


def browser_tokens(value):
    result = {"accessToken": value["accessToken"]}
    if isinstance(value.get("refreshToken"), str):
        result["refreshToken"] = value["refreshToken"]
    return result


def read_browser_credentials(path, *, private=False, preserve_metadata=False):
    # Never accept API keys or unrelated provider credentials as a substitute.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CursorError("config")
            if private and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise CursorError("auth_file_permissions")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise CursorError("config")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CursorError("config")
        if value.get("apiKey") or value.get("bedrockCredentials"):
            raise CursorError("config")
        if not isinstance(value.get("accessToken"), str) or not value["accessToken"]:
            raise CursorError("not_logged_in")
        return value if preserve_metadata else browser_tokens(value)
    except FileNotFoundError:
        raise CursorError("not_logged_in") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CursorError("config") from None


def write_credentials(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, prefix=".auth-", suffix=".json", delete=False
        ) as target:
            pending = Path(target.name)
            target.write(encode(value))
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
