"""One-command checks of each dependency. No model prompts or secret output."""

import argparse
import asyncio
import json
import re
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

from .credentials import read_browser_credentials
from .errors import CursorError
from .launcher import bwrap_argv, local_bundle, ssh_argv, ssh_error_code
from .metrics import record
from .process import run_process
from .protocol import RUNNER_CLEANUP_PROTOCOL
from .settings import settings_from


async def diagnose(cfg, *, include_raw=False):
    report = {"model_configured": bool(cfg["model"])}
    raw_results = {}

    def capture(name, code, stdout, stderr):
        # Only dependency probes: never capture authenticate/session/prompt frames.
        if include_raw:
            raw_results[name] = {
                "exit_code": code,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }

    if include_raw:
        report["raw_probes"] = raw_results
    mode = cfg["transport"]
    if mode == "auto":
        try:
            local_bundle(cfg)
            mode = "local" if Path(cfg["bubblewrap_binary"]).is_file() else "ssh"
        except CursorError:
            mode = "ssh"
    report["transport"] = mode
    with TemporaryDirectory(prefix="cursor-diagnose-") as folder:
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "LANG": "C.UTF-8"}
        if mode == "local":
            try:
                bundle = local_bundle(cfg)
                report["cli_installation"] = "ok"
                home, work = Path(folder) / "home", Path(folder) / "work"
                home.mkdir()
                work.mkdir()
                argv = bwrap_argv(cfg, bundle, home, work)
                argv = argv[: argv.index("--") + 1] + ["/usr/bin/true"]
                code, out, err = await run_process(
                    argv, b"", cwd=folder, env=env, timeout=10, output_limit=16384
                )
                capture("bubblewrap_true", code, out, err)
                report["file_isolation"] = "ok" if code == 0 else "sandbox_unavailable"
                if code == 0:
                    version_argv = argv[: argv.index("--") + 1] + [
                        "/opt/cursor/cursor-agent",
                        "--version",
                    ]
                    code, out, err = await run_process(
                        version_argv, b"", cwd=folder, env=env, timeout=10, output_limit=16384
                    )
                    capture("cli_version", code, out, err)
                    match = re.search(rb"\b\d{4}\.\d{2}\.\d{2}-[A-Za-z0-9]+\b", out)
                    report["cli_version"] = match[0].decode() if code == 0 and match else "unknown"
            except CursorError as exc:
                report["cli_installation_or_sandbox"] = exc.code
            try:
                read_browser_credentials(Path(cfg["cursor_auth_path"]))
                report["login_file"] = "ok"
            except CursorError as exc:
                report["login_file"] = exc.code
        else:
            try:
                argv = ssh_argv(cfg, diagnose=True)
                report["client_key"] = report["known_hosts_file"] = "ok"
            except CursorError as exc:
                report["ssh_configuration"] = exc.code
                record("diagnostic", transport=mode, code=exc.code)
                return report
            code, out, err = await run_process(
                [cfg["ssh_binary"], "-V"], b"", cwd=folder, env=env, timeout=5, output_limit=16384
            )
            capture("ssh_version", code, out, err)
            match = re.search(rb"OpenSSH_[0-9.p]+", err)
            report["ssh_version"] = match[0].decode() if code == 0 and match else "unknown"
            try:
                code, out, err = await run_process(
                    argv, b"", cwd=folder, env=env, timeout=20, output_limit=16384
                )
                capture("ssh_diagnose", code, out, err)
                if code:
                    report["ssh_connection"] = ssh_error_code(err)
                else:
                    report["ssh_connection"] = "ok"
                    data = json.loads(out)
                    expected = {"cli_executable", "login_volume", "cli_version", "image_sha"}
                    if not isinstance(data, dict) or not expected <= data.keys():
                        raise CursorError("protocol")
                    capability = data.get("cleanup_protocol")
                    report["runner_cleanup_protocol"] = (
                        "ok"
                        if type(capability) is int and capability == RUNNER_CLEANUP_PROTOCOL
                        else "runner_cleanup_unsupported"
                    )
                    for name in expected:
                        value = data[name]
                        # Only protocol codes or simple build versions can be displayed.
                        report[name] = (
                            value
                            if isinstance(value, str)
                            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)
                            else "unknown"
                        )
            except (ValueError, UnicodeError, RecursionError):
                report["runner_protocol"] = "protocol"
            except CursorError as exc:
                report["ssh_connection"] = exc.code
    for value in report.values():
        if (
            isinstance(value, str)
            and value not in {"ok", "local", "ssh"}
            and not re.search(r"\d", value)
        ):
            record("diagnostic", transport=mode, code=value)
    return report


def inside_health():
    from .runner import static_health

    report = static_health()
    try:
        with socket.create_connection(("127.0.0.1", 2222), timeout=2):
            report["sshd"] = "ok"
    except OSError:
        report["sshd"] = "ssh_unreachable"
    print(json.dumps(report, ensure_ascii=False))
    healthy = all(
        report.get(name) == "ok" for name in ("cli_executable", "login_volume", "image_sha", "sshd")
    )
    return 0 if healthy else 1


def main():
    parser = argparse.ArgumentParser(
        description="Diagnose Cursor dependencies without sending a model prompt."
    )
    parser.add_argument(
        "--config", help="AstrBot plugin configuration JSON (paths/settings, not a token)"
    )
    parser.add_argument("--inside", action="store_true", help="Runner container healthcheck")
    args = parser.parse_args()
    if args.inside:
        raise SystemExit(inside_health())
    try:
        raw = json.loads(Path(args.config).read_text()) if args.config else {}
        result = asyncio.run(diagnose(settings_from(raw)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError):
        print(json.dumps({"configuration": "config"}))
        raise SystemExit(1) from None
    except CursorError as exc:
        print(json.dumps({"configuration": exc.code}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
