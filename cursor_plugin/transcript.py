"""Serialize only the text context AstrBot supplies. Never mutate its history."""

from .errors import CursorError
from .protocol import MAX_CONTEXT, encode


def as_dict(value):
    if isinstance(value, dict):
        return value
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    raise CursorError("unsupported")


def text_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise CursorError("unsupported")
    parts = []
    for item in content:
        part = as_dict(item)
        if part.get("type") == "think":
            continue  # Previous hidden reasoning is not conversational text.
        if part.get("type") != "text" or not isinstance(part.get("text"), str):
            raise CursorError("unsupported")
        parts.append(part["text"])
    return "\n".join(parts)


def build_transcript(prompt=None, contexts=None, system_prompt=None, extra_parts=None):
    messages = []
    if system_prompt:
        if not isinstance(system_prompt, str):
            raise CursorError("unsupported")
        messages.append({"role": "system", "content": system_prompt})
    for item in contexts or []:
        message = as_dict(item)
        role = message.get("role")
        if (
            role not in {"system", "developer", "user", "assistant"}
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise CursorError("unsupported")
        messages.append({"role": role, "content": text_content(message.get("content"))})
    if prompt is not None and not isinstance(prompt, str):
        raise CursorError("unsupported")
    extra = text_content(extra_parts) if extra_parts else ""
    if prompt is not None or extra:
        content = prompt or ""
        if extra:
            content += ("\n" if content else "") + extra
        messages.append({"role": "user", "content": content})
    transcript = (
        "You are replying to a text chat managed by AstrBot. The JSON transcript "
        "below contains the configured persona, prior messages and current user "
        "message. Continue the conversation and output only the assistant reply. "
        "Do not inspect files, run commands, browse, use tools or manage a Cursor "
        "session. System/developer entries describe the chat persona.\n\n"
        + encode(messages).decode("utf-8")
    )
    if len(transcript.encode("utf-8")) > MAX_CONTEXT:
        raise CursorError("context_limit")
    return transcript
