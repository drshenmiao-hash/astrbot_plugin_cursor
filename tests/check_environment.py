"""Operator-run PID/concurrency/cancellation/byte/diagnostic checks in one script.

Default mode diagnoses dependencies only. --mock runs against a local fake ACP
process. --allow-model-requests enables actual Cursor requests and may incur cost.
"""

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from unittest import SkipTest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TESTS = [
    "PID reuse",
    "same-session concurrency",
    "cancel and reconnect",
    "per-turn bytes",
    "diagnostics",
]
PREFLIGHT_FAILURES = {
    "ssh_host_key_mismatch",
    "ssh_auth_denied",
    "image_sha_mismatch",
    "image_receipt_missing",
    "protocol",
    "unknown",
    "timeout",
    "startup_timeout",
    "cli_failed",
    "transport",
    "output_limit",
    "sandbox_path_conflict",
    "runner_in_use",
}


def output(status, name, raw):
    print(f"{status} {name}", flush=True)
    print("RAW " + json.dumps(raw, ensure_ascii=False, default=str), flush=True)


def agent_pid(engine):
    """Find the CLI, rather than mistaking a bubblewrap supervisor for the CLI."""
    ready = engine.client.ready
    if engine.owner.kind in {"ssh", "mock"}:
        return ready.get("agent_pid")
    pending = [ready.get("transport_pid")]
    try:
        while pending:
            pid = pending.pop()
            if not isinstance(pid, int):
                continue
            root = Path(f"/proc/{pid}")
            try:
                argv = (root / "cmdline").read_bytes().split(b"\0")
                if b"/opt/cursor/index.js" in argv and b"acp" in argv:
                    return pid
                pending.extend(int(p) for p in (root / f"task/{pid}/children").read_text().split())
            except FileNotFoundError:
                continue  # A short-lived launcher can exit during enumeration.
    except (OSError, ValueError) as exc:
        raise SkipTest(f"Cannot inspect actual CLI PID through /proc: {exc}") from exc
    raise SkipTest(f"Actual CLI PID not visible; RAW transport_pid={ready.get('transport_pid')}")


async def run(args, cfg):
    from cursor_plugin.diagnostics import diagnose
    from cursor_plugin.engine import Engine
    from cursor_plugin.errors import CursorError
    from cursor_plugin.models import resolve_model
    from cursor_plugin.transcript import render_messages

    metadata = []

    def collect(event, **values):
        metadata.append({"event": event, **values})

    if args.mock:
        from mock_runtime import MockOwner

        engine = Engine(cfg, owner_factory=MockOwner)
        diagnostic = {"transport": "mock", "notice": "CLI/login/network/billing not tested"}
    else:
        engine = Engine(cfg)
        try:
            diagnostic = await diagnose(cfg, include_raw=True)
        except Exception:
            diagnostic = {"exception": traceback.format_exc()}
    failed = 0
    blockers = {
        "ssh_missing",
        "ssh_key_missing",
        "ssh_key_permissions",
        "ssh_host_key_missing",
        "ssh_unreachable",
        "cli_missing",
        "cli_layout",
        "not_logged_in",
        "sandbox_missing",
        "sandbox_unavailable",
        "config",
    }
    missing = [
        str(value) for value in diagnostic.values() if isinstance(value, str) and value in blockers
    ]
    enabled = args.mock or args.allow_model_requests
    reason = None
    if not enabled:
        reason = (
            "Real requests disabled; pass --allow-model-requests after login/model configuration."
        )
    elif not cfg["model"]:
        reason = "Missing model_id."
    elif missing:
        reason = "Missing/unavailable environment: " + ", ".join(missing)
    if reason:
        for name in TESTS[:-1]:
            output("SKIP", name, {"missing": reason, "diagnostic": diagnostic})
        output(
            "FAIL"
            if "exception" in diagnostic
            or any(isinstance(v, str) and v in PREFLIGHT_FAILURES for v in diagnostic.values())
            else "SKIP"
            if missing
            else "PASS",
            "diagnostics",
            diagnostic,
        )
        await engine.close()
        return int(
            "exception" in diagnostic
            or any(isinstance(v, str) and v in PREFLIGHT_FAILURES for v in diagnostic.values())
        )

    preflight_errors = [
        value
        for value in diagnostic.values()
        if isinstance(value, str) and value in PREFLIGHT_FAILURES
    ]
    if preflight_errors or "exception" in diagnostic:
        for name in TESTS[:-1]:
            output(
                "SKIP",
                name,
                {
                    "missing": "Preflight failed; model requests were not started.",
                    "diagnostic": diagnostic,
                },
            )
        output("FAIL", TESTS[4], diagnostic)
        await engine.close()
        return 1

    async def send(messages, scope, sequence=None):
        return await engine.submit(messages, scope, sequence=sequence)

    async def check(name, function):
        nonlocal failed
        try:
            raw = await function()
            output("PASS", name, raw)
        except SkipTest as exc:
            output("SKIP", name, {"missing": str(exc)})
        except CursorError as exc:
            if exc.code in blockers:
                output(
                    "SKIP",
                    name,
                    {"missing": exc.code, "error": str(exc), "traceback": traceback.format_exc()},
                )
            else:
                failed += 1
                output("FAIL", name, {"error": str(exc), "traceback": traceback.format_exc()})
        except Exception:
            failed += 1
            output("FAIL", name, {"traceback": traceback.format_exc(), "runtime": engine.health()})

    async def pid_reuse():
        first = [{"role": "user", "content": "Reply OK only."}]
        reply1 = await send(first, "environment-pid")
        pid1 = agent_pid(engine)
        second = first + [
            {"role": "assistant", "content": reply1},
            {"role": "user", "content": "Again, reply OK only."},
        ]
        reply2 = await send(second, "environment-pid")
        pid2 = agent_pid(engine)
        assert type(pid1) is int and pid1 > 0 and pid1 == pid2, (pid1, pid2)
        return {
            "agent_pid_before": pid1,
            "agent_pid_after": pid2,
            "reply1": reply1,
            "reply2": reply2,
            "runtime": engine.health(),
        }

    async def concurrency():
        offset = len(metadata)
        arrivals = [3, 1, 2]
        tasks = [
            asyncio.create_task(
                send(
                    [{"role": "user", "content": f"Sequence {n}; reply OK only."}],
                    "environment-order",
                    sequence=n,
                )
            )
            for n in arrivals
        ]
        replies = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [repr(reply) for reply in replies if isinstance(reply, BaseException)]
        finished = [
            m
            for m in metadata[offset:]
            if m["event"] == "request_finished" and m.get("session") == "environment-order"
        ]
        actual = [m["sequence"] for m in finished]
        assert not errors and actual == [1, 2, 3], {
            "arrival": arrivals,
            "execution": actual,
            "errors": errors,
        }
        return {
            "arrival": arrivals,
            "execution": actual,
            "replies": replies,
            "metadata": finished,
            "boundary": "Only pending requests in the batching window can be reordered; no sequence means arrival order.",
        }

    async def cancel_reconnect():
        task = asyncio.create_task(
            send(
                [{"role": "user", "content": "CANCEL_TEST: write a very long numbered list."}],
                "environment-cancel",
            )
        )
        try:
            for _ in range(200):
                if engine.client is not None and engine.client.turns:
                    break
                if task.done():
                    raise SkipTest(
                        "Request completed before cancellation could be exercised. RAW result="
                        + repr(task.result())
                    )
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("No active prompt observed within 10 seconds")
            pid1 = agent_pid(engine)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for _ in range(200):
                if engine.active == 0:
                    break
                await asyncio.sleep(0.05)
            assert engine.active == 0, engine.health()
            # Exercise an actual reconnect, not merely another prompt on the same pipe.
            await engine.client.close()
            await engine.owner.close()
            reply = await send(
                [{"role": "user", "content": "Reply OK only."}], "environment-cancel"
            )
            pid2 = agent_pid(engine)
            assert pid1 != pid2 and reply.strip(), (pid1, pid2, reply)
            return {
                "cancelled_future": task.cancelled(),
                "forced_disconnect": True,
                "old_pid": pid1,
                "new_pid": pid2,
                "reply": reply,
            }
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def byte_statistics():
        offset = len(metadata)
        first = [{"role": "user", "content": "Reply OK only."}]
        reply1 = await send(first, "environment-bytes")
        delta = [{"role": "user", "content": "Reply OK again."}]
        second = first + [{"role": "assistant", "content": reply1}] + delta
        reply2 = await send(second, "environment-bytes")
        records = [
            m
            for m in metadata[offset:]
            if m["event"] == "request_finished" and m.get("session") == "environment-bytes"
        ]
        assert len(records) == 2 and records[1]["reused"] is True, records
        expected = len(render_messages(delta, incremental=True).encode())
        assert records[1]["sent_input_bytes"] == expected, (records, expected)
        assert records[1]["wire_bytes"] > records[1]["sent_input_bytes"], records
        return {
            "turns": records,
            "expected_incremental_sent_bytes": expected,
            "full_replay_bytes": len(render_messages(second).encode()),
            "reply2": reply2,
            "billing": "Byte counts are not token counts or billed cost.",
        }

    with patch("cursor_plugin.engine.record", collect):
        try:
            catalog = await asyncio.wait_for(
                engine.available_models(), cfg["queue_timeout_seconds"]
            )
            cfg["model"] = resolve_model(cfg["model"], catalog)
            await check(TESTS[0], pid_reuse)
            await check(TESTS[1], concurrency)
            await check(TESTS[2], cancel_reconnect)
            await check(TESTS[3], byte_statistics)
            bad = [
                value
                for value in diagnostic.values()
                if isinstance(value, str) and value in PREFLIGHT_FAILURES
            ]
            if bad or "exception" in diagnostic:
                failed += 1
                output("FAIL", TESTS[4], diagnostic)
            else:
                output("PASS", TESTS[4], diagnostic)
        finally:
            try:
                await engine.close()
            except Exception:
                failed += 1
                output("FAIL", "cleanup", {"traceback": traceback.format_exc()})
    return int(failed > 0)


def main():
    if sys.version_info < (3, 10):
        for name in TESTS:
            output("SKIP", name, {"missing": "Python 3.10 or newer is required."})
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Plugin configuration JSON")
    parser.add_argument(
        "--allow-model-requests",
        action="store_true",
        help="Actual Cursor calls; may consume subscription/paid usage",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use a fake local ACP subprocess; not a real Cursor validation",
    )
    args = parser.parse_args()
    try:
        from cursor_plugin.settings import settings_from

        raw = json.loads(Path(args.config).read_text()) if args.config else {}
        if args.mock:
            raw.update(
                model_id="mock-plan-model",
                max_concurrency=2,
                timeout_seconds=15,
                queue_timeout_seconds=30,
            )
        cfg = settings_from(raw)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        for name in TESTS:
            output("SKIP", name, {"missing": str(exc), "traceback": traceback.format_exc()})
        return 0
    try:
        return asyncio.run(run(args, cfg))
    except ImportError as exc:
        for name in TESTS:
            output(
                "SKIP", name, {"missing_dependency": str(exc), "traceback": traceback.format_exc()}
            )
        return 0
    except Exception:
        output("FAIL", "script", {"traceback": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
