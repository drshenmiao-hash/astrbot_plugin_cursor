"""One-command checks of each dependency. No model prompts or secret output."""

import argparse
import asyncio
import json
import os
import re
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import CursorError
from .launcher import (
    bwrap_argv,
    direct_cli_env,
    direct_scratch,
    inspect_local_cli,
    local_credentials,
    select_transport,
    ssh_argv,
    ssh_error_code,
)
from .process import run_process
from .protocol import RUNNER_CLEANUP_PROTOCOL
from .settings import LOCAL_TRANSPORTS, settings_from

_VERSION = re.compile(rb"\d{4}\.\d{2}\.\d{2}-[A-Za-z0-9]{1,32}")
_VERSION_TIMEOUT = 5


def safe_cli_version(value):
    if isinstance(value, str) and value.isascii() and _VERSION.fullmatch(value.encode()):
        return value
    return None


async def _local_checks(cfg, report):
    mode = report["transport"]
    path, bundle, error = inspect_local_cli(cfg)
    report["cli_installation"] = error or "ok"
    report["cli_path"] = str(path) if path else None
    report["cli_resolved_path"] = str(bundle / "cursor-agent") if bundle else None
    report["cli_source"] = "configured" if cfg["cli_binary"] else "path"
    report["cli_state"] = {
        "cli_missing": "not_installed",
        "cli_path_absolute": "unconfigured",
        "cli_layout": "installed",
        "sandbox_path_conflict": "installed",
        "cli_not_executable": "installed",
        "cli_incompatible": "incompatible",
    }.get(error, "version_unverified")
    sandbox = Path(cfg["bubblewrap_binary"])
    report["bubblewrap"] = (
        "disabled" if mode == "local_direct" else "ok" if sandbox.is_file() else "sandbox_missing"
    )
    if mode == "ssh":  # Automatic selection retained the existing SSH route.
        return

    report["file_isolation"] = "disabled" if mode == "local_direct" else report["bubblewrap"]
    try:
        local_credentials(cfg)
        report["login_file"] = "ok"
    except CursorError as exc:
        report["login_file"] = exc.code

    try:
        scratch = (
            direct_scratch()
            if mode == "local_direct"
            else TemporaryDirectory(prefix="cursor-diagnose-")
        )
    except CursorError as exc:
        report["local_runtime"] = exc.code
        return
    except OSError:
        report["local_runtime"] = "cli_incompatible"
        return

    with scratch as folder:
        home, work = Path(folder) / "home", Path(folder) / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "LANG": "C.UTF-8"}
        if mode == "local":
            try:
                argv = bwrap_argv(cfg, bundle, home, work, command=["/usr/bin/true"])
                code, _, _ = await run_process(
                    argv, b"", cwd=folder, env=env, timeout=5, output_limit=4096
                )
                report["file_isolation"] = "ok" if code == 0 else "sandbox_unavailable"
            except CursorError as exc:
                report["file_isolation"] = (
                    exc.code
                    if exc.code in {"sandbox_missing", "sandbox_path_conflict"}
                    else "sandbox_unavailable"
                )
            if report["file_isolation"] != "ok":
                return
        if bundle is None:
            return
        if mode == "local":
            argv[-1:] = ["/opt/cursor/cursor-agent", "--version"]
        else:
            env = direct_cli_env(home)
            argv = [str(bundle / "cursor-agent"), "--disable-auto-update", "--version"]
        try:
            code, out, _ = await run_process(
                argv, b"", cwd=str(work), env=env, timeout=_VERSION_TIMEOUT, output_limit=4096
            )
        except CursorError as exc:
            if exc.code == "transport":
                report["cli_installation"] = "cli_incompatible"
                report["cli_state"] = "incompatible"
            return
        if code:
            report["cli_installation"] = "cli_incompatible"
            report["cli_state"] = "incompatible"
        elif version := safe_cli_version(out.decode("ascii", errors="replace").strip()):
            report["cli_version"] = version
            report["cli_state"] = "executable"


async def _ssh_checks(cfg, report):
    try:
        argv = ssh_argv(cfg, diagnose=True)
        report["client_key"] = report["known_hosts_file"] = "ok"
    except (OSError, CursorError) as exc:
        report["ssh_configuration"] = exc.code if isinstance(exc, CursorError) else "config"
        return
    with TemporaryDirectory(prefix="cursor-diagnose-") as folder:
        env = {"PATH": "/usr/bin:/bin", "HOME": folder, "LANG": "C.UTF-8"}
        try:
            code, _, err = await run_process(
                [cfg["ssh_binary"], "-V"],
                b"",
                cwd=folder,
                env=env,
                timeout=3,
                output_limit=4096,
            )
            match = re.search(rb"OpenSSH_[0-9.p]{1,16}", err)
            if code == 0 and match:
                report["ssh_version"] = match[0].decode()
        except CursorError:
            pass
        try:
            code, out, err = await run_process(
                argv, b"", cwd=folder, env=env, timeout=20, output_limit=16384
            )
            if code:
                report["ssh_connection"] = ssh_error_code(err)
                return
            report["ssh_connection"] = "ok"
            data = json.loads(out)
            if (
                not isinstance(data, dict)
                or not {"cli_executable", "login_volume", "cli_version", "image_sha"} <= data.keys()
            ):
                raise ValueError
            capability = data.get("cleanup_protocol")
            report["runner_cleanup_protocol"] = (
                "ok"
                if type(capability) is int and capability == RUNNER_CLEANUP_PROTOCOL
                else "runner_cleanup_unsupported"
            )
            for key, codes in (
                ("cli_executable", ("ok", "cli_missing")),
                ("login_volume", ("ok", "not_logged_in", "config")),
                ("image_sha", ("ok", "image_sha_mismatch", "image_receipt_missing")),
            ):
                value = data[key]
                report[key] = value if value in codes else "protocol"
            report["cli_version"] = safe_cli_version(data["cli_version"])
        except (ValueError, UnicodeError, RecursionError):
            report["runner_protocol"] = "protocol"
        except CursorError as exc:
            report["ssh_connection"] = exc.code


async def diagnose(cfg):
    """Inspect dependencies without opening ACP or acquiring any login lock."""
    mode = select_transport(cfg)
    report = {
        "transport": mode,
        "configured_transport": cfg["transport"],
        "model_configured": bool(cfg["requested_model"]),
        "root_warning": mode == "local_direct" and os.geteuid() == 0,
    }
    if mode in LOCAL_TRANSPORTS or cfg["transport"] == "auto":
        await _local_checks(cfg, report)
    if mode == "ssh":
        await _ssh_checks(cfg, report)
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
