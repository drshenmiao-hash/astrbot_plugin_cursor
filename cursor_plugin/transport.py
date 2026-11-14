"""SSH is only a pipe to a forced CLI runner, never an HTTP model bridge."""

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import CursorError
from .process import run_process
from .protocol import MAX_INPUT, MAX_OUTPUT, VERSION, encode, explicit_model, parse_reply


def settings_from(config):
    try:
        host = config.get("ssh_host", "cursor-cli")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
            raise ValueError
        port = int(config.get("ssh_port", 2222))
        timeout = int(config.get("timeout_seconds", 120))
        if not 1 <= port <= 65535 or not 1 <= timeout <= 600:
            raise ValueError
        paths = {}
        for field, default in (
            ("ssh_binary", "/usr/bin/ssh"),
            ("identity_file", "/run/cursor-client/id_ed25519"),
            ("known_hosts_file", "/run/cursor-client/known_hosts"),
        ):
            value = config.get(field, default)
            if (
                not isinstance(value, str)
                or not Path(value).is_absolute()
                or any(c in value for c in "\r\n\x00")
            ):
                raise ValueError
            paths[field] = value
        model = config.get("model_id", "")
        # Empty is permitted at install time. Requests still fail closed.
        if model:
            explicit_model(model)
        return {"host": host, "port": port, "timeout": timeout, "model": model, **paths}
    except (TypeError, ValueError, OverflowError):
        raise CursorError("config") from None


class SSHTransport:
    def __init__(self, settings):
        self.settings = settings

    async def complete(self, model, transcript):
        cfg = self.settings
        explicit_model(model)
        if model != cfg["model"]:
            raise CursorError("model")
        for name in ("ssh_binary", "identity_file", "known_hosts_file"):
            if not Path(cfg[name]).is_file():
                raise CursorError("config")
        argv = [
            cfg["ssh_binary"],
            "-F",
            "/dev/null",
            "-T",
            "-p",
            str(cfg["port"]),
            "-i",
            cfg["identity_file"],
        ]
        for option in (
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=/dev/null",
            f"UserKnownHostsFile={cfg['known_hosts_file']}",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "ForwardX11=no",
            "RequestTTY=no",
            "ConnectTimeout=10",
            "ServerAliveInterval=5",
            "ServerAliveCountMax=2",
            "LogLevel=ERROR",
        ):
            argv.extend(["-o", option])
        argv.extend(["-l", "cursor", cfg["host"]])
        payload = encode(
            {"version": VERSION, "model": model, "prompt": transcript, "timeout": cfg["timeout"]}
        )
        if len(payload) > MAX_INPUT:
            raise CursorError("context_limit")
        with TemporaryDirectory(prefix="astrbot-cursor-ssh-") as work:
            # No SSH agent, proxy config, API keys, Python/Node injection or HOME
            # from AstrBot reaches even the local SSH subprocess.
            code, out, _ = await run_process(
                argv,
                payload,
                cwd=work,
                env={"PATH": "/usr/bin:/bin", "HOME": work, "LANG": "C.UTF-8"},
                timeout=cfg["timeout"] + 15,
                output_limit=MAX_OUTPUT,
            )
        if code != 0:
            raise CursorError("transport")
        return parse_reply(out)
