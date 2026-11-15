"""Executed offline checks: pure functions only, with visible inputs and outputs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cursor_plugin.logic import classify_error, history_plan, queue_execution_order


def messages(*texts):
    return [{"role": "user", "content": text} for text in texts]


cases = [
    ("append", messages("A", "B", "C"), messages("A", "B", "C", "D"), True, ["D"]),
    (
        "middle-only-change",
        messages("A", "B", "C", "D"),
        messages("A", "B!", "C", "D"),
        False,
        ["A", "B!", "C", "D"],
    ),
    (
        "tail-punctuation",
        messages("你好。", "回答。"),
        messages("你好。", "回答！"),
        False,
        ["你好。", "回答！"],
    ),
    ("only-one-remains", messages("A", "B", "C"), messages("C"), False, ["C"]),
    ("identical-one", messages("A"), messages("A"), True, []),
    ("new-history", [], messages("first"), False, ["first"]),
    ("empty-history", [], [], False, []),
]
expected_rules = {
    "append": ("grown", "match", "match"),
    "middle-only-change": ("same", "changed", "match"),
    "tail-punctuation": ("same", "changed", "changed"),
    "only-one-remains": ("shrunk", "changed", "insufficient"),
    "identical-one": ("same", "match", "match"),
    "new-history": ("grown", "match", "empty"),
    "empty-history": ("same", "match", "empty"),
}
for name, old, new, reused, expected in cases:
    result = history_plan(old, new)
    actual = [item["content"] for item in result["delta"]]
    assert result["reused"] is reused and actual == expected, (name, result)
    assert (
        tuple(result[key] for key in ("length_rule", "hash_rule", "tail_rule"))
        == expected_rules[name]
    ), (name, result)
    print(
        f"PASS history/{name}: old={[m['content'] for m in old]!r} new={[m['content'] for m in new]!r} => delta={actual!r}; reuse={result['reused']}; length={result['length_rule']}; hash={result['hash_rule']}; tail={result['tail_rule']}"
    )

pending = [
    {"id": "a3", "session": "A", "sequence": 3},
    {"id": "a1", "session": "A", "sequence": 1},
    {"id": "a2", "session": "A", "sequence": 2},
]
ordered = queue_execution_order(pending)
assert ordered == ["a1", "a2", "a3"], ordered
print(f"PASS queue/out-of-order: arrival={[x['id'] for x in pending]} => execution={ordered}")
active_order = queue_execution_order(
    pending + [{"id": "b1", "session": "B", "sequence": 1}], active_sessions={"A"}
)
assert active_order == ["b1"], active_order
print(f"PASS queue/session-active: active=A => executable={active_order}")
no_sequence = [{"id": "first-arrived", "session": "A"}, {"id": "second-arrived", "session": "A"}]
assert queue_execution_order(no_sequence) == ["first-arrived", "second-arrived"]
print(
    "PASS queue/no-sequence: execution=[first-arrived, second-arrived]; chronology cannot be inferred"
)

errors = [
    ("Host key verification failed.", "ssh", "ssh_host_key_mismatch"),
    ("Permission denied (publickey).", "ssh", "ssh_auth_denied"),
    (
        "bwrap: Creating new namespace failed: Operation not permitted",
        "startup",
        "sandbox_unavailable",
    ),
    ("Authentication required. PRIVATE_TOKEN_SENTINEL", "request", "not_logged_in"),
    ("Queue wait timed out", "request", "queue_timeout"),
    ("connect to host: Connection timed out", "ssh", "ssh_unreachable"),
    ("Model example-model is not available", "request", "model_unavailable"),
    ("429 rate limit exceeded", "request", "rate_limited"),
    ("Quota exceeded", "request", "quota_exhausted"),
    ("Context too long", "request", "context_limit"),
    ("Invalid JSON response", "request", "protocol"),
    ("request timed out", "request", "timeout"),
    ("unrecognized failure", "request", "cli_failed"),
]
for raw, stage, expected in errors:
    actual = classify_error(raw, stage=stage)
    assert actual == expected, (raw, actual, expected)
    print(f"PASS error: raw={raw!r} stage={stage} => {actual}")
print("RESULT: 23 pure-logic checks PASS; network/process/SSH calls: 0")
