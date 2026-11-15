"""Metadata-only records. No request/response content, credentials, paths or raw errors."""

import json
import logging

logger = logging.getLogger("astrbot")
_FIELDS = {
    "request_id",
    "session",
    "event",
    "transport",
    "code",
    "input_bytes",
    "sent_input_bytes",
    "wire_bytes",
    "output_bytes",
    "queue_ms",
    "duration_ms",
    "reused",
    "queue_depth",
    "active",
    "reset_reason",
    "process_reused",
    "version",
    "sequence",
}


def record(event, **values):
    data = {"event": event}
    data.update({key: value for key, value in values.items() if key in _FIELDS})
    logger.info("cursor_provider %s", json.dumps(data, ensure_ascii=True, separators=(",", ":")))
