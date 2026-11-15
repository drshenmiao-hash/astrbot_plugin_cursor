"""Validated runtime options. No command strings or arbitrary environment injection."""

import re
from pathlib import Path

from .errors import CursorError


def settings_from(config):
    try:
        mode = config.get("transport", "auto")
        if mode not in {"auto", "local", "ssh"}:
            raise ValueError
        host = config.get("ssh_host", "cursor-cli")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError
        options = {"transport": mode, "host": host, "model": config.get("model_id", "")}
        options["protected_paths"] = [str(Path(__file__).resolve().parents[1])]
        if not isinstance(options["model"], str):
            raise ValueError
        if options["model"].strip().lower() in {"", "auto", "auto-smart", "default"}:
            # Install the source, but let initialize() report that no explicit
            # model was selected. Never substitute a model for these values.
            options["model"] = ""
        if len(options["model"]) > 256 or any(ord(c) < 32 for c in options["model"]):
            raise ValueError
        options["requested_model"] = options["model"]
        for name, default, low, high in (
            ("ssh_port", 2222, 1, 65535),
            ("timeout_seconds", 120, 1, 1800),
            ("queue_timeout_seconds", 120, 1, 3600),
            ("max_concurrency", 2, 1, 8),
            ("queue_capacity", 64, 1, 1024),
            ("context_max_bytes", 262144, 1024, 16777216),
            ("output_max_bytes", 2097152, 1024, 16777216),
            ("max_sessions", 128, 1, 4096),
        ):
            value = config.get(name, default)
            if isinstance(value, bool):
                raise ValueError
            value = int(value)
            if not low <= value <= high:
                raise ValueError
            options[name] = value
        for name, default in (
            ("ssh_binary", "/usr/bin/ssh"),
            ("identity_file", "/run/cursor-client/id_ed25519"),
            ("known_hosts_file", "/run/cursor-client/known_hosts"),
            ("cli_binary", ""),
            ("bubblewrap_binary", "/usr/bin/bwrap"),
            ("cursor_auth_path", "~/.config/cursor/auth.json"),
        ):
            value = config.get(name, default)
            if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
                raise ValueError
            if value:
                value = str(Path(value).expanduser())
                if not Path(value).is_absolute():
                    raise ValueError
            options[name] = value
        return options
    except (TypeError, ValueError, OverflowError):
        raise CursorError("config") from None
