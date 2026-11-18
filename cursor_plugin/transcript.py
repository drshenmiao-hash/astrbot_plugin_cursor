"""Canonical text history and an incremental rendering; AstrBot remains authoritative."""

from .errors import CursorError, UnsupportedFeatureError
from .protocol import MAX_CONTEXT, encode


def as_dict(value):
    if isinstance(value, dict):
        return value
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    raise UnsupportedFeatureError("content")


def text_content(content, *, read_image=None):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise UnsupportedFeatureError("content")
    parts = []
    for item in content:
        part = as_dict(item)
        kind = part.get("type")
        if kind == "think":
            continue
        if kind in {"image", "image_url"}:
            if read_image is None:
                raise UnsupportedFeatureError("image")
            source = part.get("image_url")
            if isinstance(source, dict):
                source = source.get("url")
            read_image(source)
            continue
        if kind in {"audio", "audio_url"}:
            raise UnsupportedFeatureError("audio")
        if kind != "text" or not isinstance(part.get("text"), str):
            raise UnsupportedFeatureError("content")
        parts.append(part["text"])
    return "\n".join(parts)


def normalize_messages(
    prompt=None,
    contexts=None,
    system_prompt=None,
    extra_parts=None,
    *,
    max_bytes=MAX_CONTEXT,
    image_input=None,
    image_urls=None,
):
    def user_content(content, refs=()):
        images = []

        def read(source):
            images.append(image_input.read(source))

        text = text_content(content, read_image=read if image_input is not None else None)
        for ref in refs:
            if image_input is None:
                raise UnsupportedFeatureError("image")
            read(ref)
        return text, images

    messages = []
    if system_prompt:
        if not isinstance(system_prompt, str):
            raise UnsupportedFeatureError("content")
        messages.append({"role": "system", "content": system_prompt})
    for item in contexts or []:
        message = as_dict(item)
        role = message.get("role")
        if role == "tool" or message.get("tool_calls") or message.get("function_call"):
            raise UnsupportedFeatureError("tool_calls")
        if role not in {"system", "developer", "user", "assistant"}:
            raise UnsupportedFeatureError("content")
        if role == "user":
            content, images = user_content(message.get("content"))
        else:
            content, images = text_content(message.get("content")), []
        entry = {"role": role, "content": content}
        if images:
            entry["images"] = images
        messages.append(entry)
    if prompt is not None and not isinstance(prompt, str):
        raise UnsupportedFeatureError("content")
    if (
        image_input is not None
        and isinstance(extra_parts, list)
        and any(as_dict(part).get("text") == "[Image unavailable]" for part in extra_parts)
    ):
        raise CursorError("image_source")
    extra, images = user_content(extra_parts, image_urls or ())
    if prompt is not None or extra or images:
        content = prompt or ""
        if extra:
            content += ("\n" if content else "") + extra
        entry = {"role": "user", "content": content}
        if images:
            entry["images"] = images
        messages.append(entry)
    if not messages:
        raise CursorError("empty_input")
    if len(encode(messages)) > max_bytes:
        raise CursorError("context_limit")
    return messages


def render_messages(messages, *, incremental=False, clock_fields=None, image_count=0):
    if incremental:
        header = "Continue the conversation with these new transcript entries. Output only the assistant reply.\n\n"
    else:
        header = (
            "You are replying to a text chat managed by AstrBot. The JSON transcript "
            "below contains the configured persona, prior messages and current user "
            "message. Continue the conversation and output only the assistant reply. "
            "Do not inspect files, run commands, browse, use tools or manage a Cursor "
            "session. System/developer entries describe the chat persona.\n\n"
        )
    if incremental and clock_fields:
        header = (
            header.rstrip("\n")
            + "\nCurrent clock fields for this request (supersede earlier clock values only): "
            + encode(clock_fields).decode("utf-8")
            + "\n\n"
        )
    if image_count:
        header += "In the transcript, each images index refers to the corresponding image attachment (1-based).\n\n"
    return header + encode(messages).decode("utf-8")


def build_transcript(
    prompt=None, contexts=None, system_prompt=None, extra_parts=None, *, max_bytes=MAX_CONTEXT
):
    return render_messages(
        normalize_messages(prompt, contexts, system_prompt, extra_parts, max_bytes=max_bytes)
    )
