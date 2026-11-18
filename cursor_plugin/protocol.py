"""Versioned stdin/stdout envelope, with bounded input and explicit models."""

import json
import re

from .errors import CursorError

VERSION = 1
RUNNER_CLEANUP_PROTOCOL = 1
# Reserved SSH exit status returned only after the runner exits its lock scope.
RUNNER_CLEANUP_CONFIRMED = 42
MAX_INPUT = 512 * 1024
MAX_CONTEXT = 256 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def explicit_model(value):
    if (
        not isinstance(value, str)
        or not MODEL_PATTERN.fullmatch(value)
        or value.lower() in {"auto", "auto-smart", "default"}
    ):
        raise CursorError("model")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise CursorError("protocol") from None
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise CursorError("protocol")
    return value


def parse_reply(raw):
    value = decode(raw)
    if value.get("ok") is not True:
        code = value.get("code")
        raise CursorError(code if isinstance(code, str) else "protocol")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise CursorError("protocol")
    return text
