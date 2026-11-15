"""Pure history, queue and error rules. No IO, network, processes or framework imports."""

import hashlib
import json


def history_hashes(history):
    return [
        hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in history
    ]


def compare_history_hashes(old_hashes, new_hashes, *, tail_count=2):
    if tail_count < 1:
        raise ValueError("tail_count must be positive")
    old_size, new_size = len(old_hashes), len(new_hashes)
    prefix = list(new_hashes[:old_size])
    length_rule = "grown" if new_size > old_size else "shrunk" if new_size < old_size else "same"
    hash_rule = "match" if list(old_hashes) == prefix else "changed"
    tail_size = min(tail_count, old_size)
    if new_size < old_size:
        tail_rule = "insufficient"
    elif tail_size == 0:
        tail_rule = "empty"
    else:
        tail_rule = "match" if list(old_hashes[-tail_size:]) == prefix[-tail_size:] else "changed"
    # Full dialogue-prefix equality is authoritative. Length/tail are diagnostic evidence,
    # never substitutes for checking middle messages.
    reused = old_size > 0 and hash_rule == "match"
    reason = (
        "continuous_history"
        if reused
        else "new_conversation"
        if not old_size
        else "history_shrunk"
        if new_size < old_size
        else "history_changed"
    )
    return {
        "reused": reused,
        "delta_start": old_size if reused else 0,
        "reason": reason,
        "length_rule": length_rule,
        "hash_rule": hash_rule,
        "tail_rule": tail_rule,
    }


def history_state(history):
    """Keep system separately; tolerate strip() of assistant replies in history.

    Only assistant boundary whitespace is canonicalized, and only for matching.
    User text (including injected reminders) and transmitted messages stay intact.
    """
    systems = [dict(item) for item in history if item.get("role") == "system"]
    dialogue = [item for item in history if item.get("role") != "system"]
    canonical = [
        {**item, "content": item["content"].strip()}
        if item.get("role") == "assistant" and isinstance(item.get("content"), str)
        else item
        for item in dialogue
    ]
    return {"systems": systems, "dialogue_hashes": history_hashes(canonical)}


def system_delta(old_systems, new_systems):
    """One transcript entry containing changes to the last applied system state."""
    operations = [
        {"op": "remove", "index": index}
        for index in range(len(old_systems) - 1, len(new_systems) - 1, -1)
    ]
    for index, new in enumerate(new_systems):
        content = new.get("content", "")
        if index >= len(old_systems):
            operations.append({"op": "add", "index": index, "content": content})
            continue
        old = old_systems[index].get("content", "")
        if content == old:
            continue
        if isinstance(old, str) and isinstance(content, str) and old and content.startswith(old):
            operations.append({"op": "append", "index": index, "content": content[len(old) :]})
        else:
            operations.append({"op": "replace", "index": index, "content": content})
    if not operations:
        return None
    return {
        "role": "system",
        "content": (
            "Update the previous system entries of this transcript before answering. "
            "Indices are zero-based among system entries. Apply these operations in order: "
            "append extends an entry; replace supersedes its previous content; "
            "add introduces an entry; remove withdraws it. Unchanged entries remain in force.\n"
            + json.dumps(operations, ensure_ascii=False, separators=(",", ":"))
        ),
    }


def reconcile_history(previous, new_history, *, last_request_hashes=None, tail_count=2):
    """Match only dialogue; return pending system updates alongside new dialogue."""
    state = history_state(new_history)
    dialogue = [item for item in new_history if item.get("role") != "system"]
    result = compare_history_hashes(
        previous["dialogue_hashes"] if previous else [],
        state["dialogue_hashes"],
        tail_count=tail_count,
    )
    # A retry may repeat the input *without* our already-generated assistant reply.
    # Match that request too, rather than reopening a session or generating twice.
    if previous and last_request_hashes and state["dialogue_hashes"] == last_request_hashes:
        result.update(reused=True, delta_start=len(dialogue), reason="request_replay")
    dialogue_delta = dialogue[result["delta_start"] :]
    update = system_delta(previous["systems"], state["systems"]) if previous else None
    delta = ([update] if update else []) + dialogue_delta if result["reused"] else list(new_history)
    return {
        **result,
        "delta": delta,
        "system_changed": update is not None,
        "has_new_user_input": any(item.get("role") == "user" for item in dialogue_delta),
    }


def history_plan(old_history, new_history, *, tail_count=2):
    return reconcile_history(history_state(old_history), new_history, tail_count=tail_count)


def queue_execution_order(requests, *, active_sessions=()):
    """Order a pending batch; explicit sequence applies only within a session.

    Without a sequence, arrival order is used. This cannot reorder requests that
    already started or infer a sequence absent from the input.
    """
    active = set(active_sessions)
    groups = {}
    for index, item in enumerate(requests):
        session = item["session"]
        groups.setdefault(session, []).append((index, item))
    for items in groups.values():
        sequences = [item.get("sequence") for _, item in items]
        if all(type(seq) is int for seq in sequences):
            # Equal sequence values retain arrival order; never kill a worker
            # because a caller supplied duplicate timestamps/sequence values.
            items.sort(key=lambda entry: entry[1]["sequence"])
    ordered = []
    while groups:
        candidates = [
            (min(index for index, _ in items), session)
            for session, items in groups.items()
            if session not in active
        ]
        if not candidates:
            break
        _, session = min(candidates, key=lambda entry: entry[0])
        _, item = groups[session].pop(0)
        ordered.append(item["id"])
        if not groups[session]:
            groups.pop(session)
    return ordered


def classify_error(raw, *, stage="request"):
    text = raw.casefold()
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return "ssh_host_key_mismatch"
    if "bwrap" in text and any(
        term in text for term in ("operation not permitted", "permission denied", "user namespace")
    ):
        return "sandbox_unavailable"
    if "permission denied (publickey" in text:
        return "ssh_auth_denied"
    if "queue" in text and any(term in text for term in ("timeout", "timed out", "expired")):
        return "queue_timeout"
    if any(
        term in text
        for term in (
            "authentication required",
            "not authenticated",
            "not logged in",
            "unauthenticated",
            "token expired",
            "token has expired",
            "invalid access token",
            "unauthorized",
        )
    ):
        return "not_logged_in"
    if any(term in text for term in ("quota exceeded", "usage limit", "insufficient balance")):
        return "quota_exhausted"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    if "model" in text and any(
        term in text for term in ("not available", "not found", "unsupported", "unavailable")
    ):
        return "model_unavailable"
    if "context" in text and any(
        term in text for term in ("too long", "too large", "limit", "exceed")
    ):
        return "context_limit"
    if any(
        term in text for term in ("invalid json", "malformed json", "json parse", "protocol error")
    ):
        return "protocol"
    if stage == "ssh" and any(
        term in text
        for term in (
            "connection refused",
            "no route",
            "resolve hostname",
            "timed out",
            "connection reset",
        )
    ):
        return "ssh_unreachable"
    if "timeout" in text or "timed out" in text:
        return "startup_timeout" if stage == "startup" else "timeout"
    return "cli_failed"
