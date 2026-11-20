"""Resolve explicit model identities against the ACP catalog; never choose a fallback."""

import json

from .errors import CursorError
from .protocol import MODEL_PATTERN, explicit_model

# An observed management ID, not a general rule for discarding effort/version
# suffixes. A candidate must still exist in this account's ACP catalog.
_MANAGEMENT_ALIASES = {"cursor-grok-4.6-high": "grok-4.6"}


class ModelResolutionError(CursorError):
    def __init__(self, code, available_ids, candidates=()):
        super().__init__(code)
        self.available_model_ids = tuple(sorted(available_ids))
        self.candidates = tuple(sorted(candidates))
        detail = " ACP 可用 modelId：" + json.dumps(self.available_model_ids, ensure_ascii=True)
        if candidates:
            detail += "；匹配存在歧义：" + json.dumps(self.candidates, ensure_ascii=True)
        self.args = (str(self) + detail,)


def catalog_ids(available_models):
    if not isinstance(available_models, list):
        raise CursorError("protocol")
    ids = set()
    for item in available_models:
        if not isinstance(item, dict):
            raise CursorError("protocol")
        model_id = item.get("modelId")
        if not isinstance(model_id, str) or not MODEL_PATTERN.fullmatch(model_id):
            raise CursorError("protocol")
        ids.add(model_id)
    return sorted(ids)


def resolve_model(requested, available_models, *, resolve_alias=True):
    ids = catalog_ids(available_models)
    if not isinstance(requested, str) or requested.strip().casefold() in {
        "",
        "auto",
        "auto-smart",
        "default",
    }:
        raise ModelResolutionError("model", ids)
    # Exact IDs always win, even if a different entry has the same display name.
    if requested in ids:
        return requested
    if not resolve_alias:
        raise ModelResolutionError("model_unavailable", ids)
    candidates = {
        item["modelId"]
        for item in available_models
        if any(
            isinstance(item.get(field), str)
            and item[field].strip().casefold() == requested.strip().casefold()
            for field in ("name", "displayName")
        )
    }
    alias = _MANAGEMENT_ALIASES.get(requested)
    if alias in ids:
        candidates.add(alias)
    # Explicit cursor-<modelId> spellings must match a full ACP ID. Do not strip
    # arbitrary suffixes or match substrings/fuzzy names.
    candidates.update(model_id for model_id in ids if requested == "cursor-" + model_id)
    if len(candidates) == 1:
        selected = candidates.pop()
        try:
            return explicit_model(selected)
        except CursorError:
            raise ModelResolutionError("model", ids) from None
    if candidates:
        raise ModelResolutionError("model_ambiguous", ids, candidates)
    raise ModelResolutionError("model_unavailable", ids)
