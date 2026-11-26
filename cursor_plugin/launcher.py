"""Launch one persistent, isolated ACP process locally or over pinned SSH."""

import asyncio
import fcntl
import json
import os
import signal
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from .credentials import DENY, browser_tokens, cli_env, read_browser_credentials, write_credentials
from .errors import CursorError
from .logic import classify_error
from .metrics import record
from .process import run_process, wait_for_cleanup
from .protocol import RUNNER_CLEANUP_CONFIRMED, RUNNER_CLEANUP_PROTOCOL
from .settings import LOCAL_TRANSPORTS


def ssh_argv(cfg, *, diagnose=False):
    if not Path(cfg["ssh_binary"]).is_file():
        raise CursorError("ssh_missing")
    identity = Path(cfg["identity_file"])
    if not identity.is_file():
        raise CursorError("ssh_key_missing")
    if identity.stat().st_mode & 0o077:
        raise CursorError("ssh_key_permissions")
    if not Path(cfg["known_hosts_file"]).is_file():
        raise CursorError("ssh_host_key_missing")
    argv = [
        cfg["ssh_binary"],
        "-F",
        "/dev/null",
        "-T",
        "-p",
        str(cfg["ssh_port"]),
        "-i",
        str(identity),
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
    if diagnose:
        argv.append("diagnose")
    return argv


def ssh_error_code(raw):
    code = classify_error(raw.decode("utf-8", errors="replace"), stage="ssh")
    return "ssh_unreachable" if code == "cli_failed" else code


def _bundle_at(command, cfg):
    if not command.is_absolute():
        raise CursorError("cli_path_absolute")
    if not command.is_file():
        raise CursorError("cli_missing")
    if not os.access(command, os.X_OK):
        raise CursorError("cli_not_executable")
    target = command.resolve()
    bundle = target.parent
    if bundle in {Path("/"), Path.home().resolve()}:
        raise CursorError("cli_layout")
    for protected in cfg.get("protected_paths", []):
        protected_path = Path(protected).resolve()
        if protected_path == bundle or bundle in protected_path.parents:
            raise CursorError("sandbox_path_conflict")
    if target.name != "cursor-agent" or not all(
        (bundle / name).is_file() and (bundle / name).resolve().parent == bundle
        for name in ("cursor-agent", "node", "index.js")
    ):
        raise CursorError("cli_layout")
    if not os.access(bundle / "node", os.X_OK):
        raise CursorError("cli_not_executable")
    if not os.access(bundle / "index.js", os.R_OK):
        raise CursorError("cli_incompatible")
    return bundle


def inspect_local_cli(cfg):
    configured = cfg["cli_binary"]
    paths = (
        [Path(configured)]
        if configured
        else (
            Path(folder) / name
            for name in ("agent", "cursor-agent")
            for folder in os.get_exec_path()
            if Path(folder).is_absolute()
        )
    )
    first_error = (None, None, "cli_missing")
    for path in paths:
        try:
            return path, _bundle_at(path, cfg), None
        except CursorError as exc:
            code = exc.code
        except (OSError, RuntimeError):
            code = "cli_incompatible"
        if configured or (code != "cli_missing" and first_error[0] is None):
            first_error = (path, None, code)
    return first_error


def local_bundle(cfg):
    _, bundle, error = inspect_local_cli(cfg)
    if error:
        raise CursorError(error)
    return bundle


def select_transport(cfg):
    if cfg["transport"] != "auto":
        return cfg["transport"]
    # Preserve the legacy choice. A configured local sandbox that fails to
    # start reports its error; only missing local components select SSH.
    try:
        local_bundle(cfg)
    except CursorError:
        return "ssh"
    return "local" if Path(cfg["bubblewrap_binary"]).is_file() else "ssh"


def bwrap_argv(cfg, bundle, home, work, *, command=None):
    bwrap = cfg["bubblewrap_binary"]
    if not Path(bwrap).is_file():
        raise CursorError("sandbox_missing")
    if not os.access(bwrap, os.X_OK):
        raise CursorError("sandbox_unavailable")
    runtime_roots = [
        Path(p) for p in ("/usr/bin", "/usr/lib", "/lib", "/lib64") if Path(p).exists()
    ]
    for protected in cfg.get("protected_paths", []):
        target = Path(protected).resolve()
        if any(
            target == root.resolve() or root.resolve() in target.parents for root in runtime_roots
        ):
            raise CursorError("sandbox_path_conflict")
    argv = [
        bwrap,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
    ]
    for root in runtime_roots:
        argv.extend(["--ro-bind", str(root), str(root)])
    argv.extend(
        ["--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    )
    for path in (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/ssl/certs",
        "/etc/ssl/openssl.cnf",
    ):
        if Path(path).exists():
            argv.extend(["--ro-bind", path, path])
    if bundle is not None:
        argv.extend(["--ro-bind", str(bundle), "/opt/cursor"])
    argv.extend(
        [
            "--bind",
            str(home),
            "/home/cursor",
            "--bind",
            str(work),
            "/work",
            "--chdir",
            "/work",
        ]
    )
    for name, value in cli_env("/home/cursor").items():
        argv.extend(["--setenv", name, value])
    argv.extend(["--", *(command or ["/opt/cursor/cursor-agent", "acp"])])
    return argv


def direct_scratch():
    # The service's TMPDIR can itself be a project. Avoid inheriting it and
    # reject known project markers above the otherwise empty workspace.
    base = Path("/tmp").resolve()
    markers = (
        ".git/HEAD",
        ".cursor",
        ".agents/skills",
        ".claude",
        "AGENTS.md",
        "CLAUDE.md",
        ".mcp.json",
        "mcp.json",
    )
    for parent in (base, *base.parents):
        git = parent / ".git"
        if (
            git.is_file()
            or git.is_symlink()
            or any((marker := parent / name).exists() or marker.is_symlink() for name in markers)
        ):
            raise CursorError("direct_workspace")
    return TemporaryDirectory(dir=base, prefix="astrbot-cursor-runtime-")


def direct_cli_env(home):
    temporary = home / "tmp"
    temporary.mkdir(mode=0o700)
    return {
        **cli_env(home),
        "TMPDIR": str(temporary),
        "XDG_RUNTIME_DIR": str(temporary),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CONFIG_DIRS": str(home / ".config"),
        "XDG_DATA_DIRS": str(home / ".local/share"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "CURSOR_AGENT_DISABLE_DEBUG_LOG": "1",
    }


def local_auth_path(cfg):
    if not cfg["cursor_auth_path"]:
        raise CursorError("not_logged_in")
    path = Path(cfg["cursor_auth_path"])
    if cfg["transport"] == "local_direct":
        try:
            parent = path.parent.stat()
        except FileNotFoundError:
            raise CursorError("not_logged_in") from None
        except OSError:
            raise CursorError("auth_file_permissions") from None
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or parent.st_mode & 0o022
            or not os.access(path.parent, os.W_OK | os.X_OK)
        ):
            raise CursorError("auth_file_permissions")
    return path


def local_credentials(cfg):
    path = local_auth_path(cfg)
    direct = cfg["transport"] == "local_direct"
    return read_browser_credentials(path, private=direct, preserve_metadata=direct)


class ProcessOwner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self._close_task = None
        self.scratch = None
        self.lock = None
        self.kind = None
        self.master_auth = None
        self.temporary_auth = None
        self.original_auth = None

    async def start(self):
        cfg = self.cfg
        mode = select_transport(cfg)
        self.kind = mode
        ready = None
        try:
            self.scratch = (
                direct_scratch()
                if mode == "local_direct"
                else TemporaryDirectory(prefix="astrbot-cursor-runtime-")
            )
            root = Path(self.scratch.name)
            cwd = root
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8"}
            if mode in LOCAL_TRANSPORTS:
                bundle = local_bundle(cfg)
                self.master_auth = local_auth_path(cfg)
                try:
                    fd = os.open(
                        self.master_auth.parent / ".astrbot-provider.lock",
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                        0o600,
                    )
                except FileNotFoundError:
                    raise CursorError("not_logged_in") from None
                self.lock = os.fdopen(fd, "r+b")
                lock_info = os.fstat(fd)
                if not stat.S_ISREG(lock_info.st_mode):
                    raise CursorError("config")
                if mode == "local_direct" and (
                    lock_info.st_uid != os.geteuid() or lock_info.st_mode & 0o077
                ):
                    raise CursorError("auth_file_permissions")
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise CursorError("runner_in_use") from None
                # Read only after obtaining the lock, so the previous owner can
                # finish persisting its refreshed session before we copy it.
                self.original_auth = local_credentials(cfg)
                home, work = root / "home", root / "work"
                home.mkdir(mode=0o700)
                work.mkdir(mode=0o700)
                self.temporary_auth = home / ".config/cursor/auth.json"
                write_credentials(self.temporary_auth, browser_tokens(self.original_auth))
                (self.temporary_auth.parent / "cli-config.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "editor": {"vimMode": False},
                            "approvalMode": "allowlist",
                            "permissions": {"allow": [], "deny": DENY},
                            "maxMode": False,
                        }
                    )
                )
                if mode == "local":
                    argv = bwrap_argv(cfg, bundle, home, work)
                    ready = {"cwd": "/work", "transport": mode}
                else:
                    env, cwd = direct_cli_env(home), work
                    argv = [str(bundle / "cursor-agent"), "--disable-auto-update", "acp"]
                    ready = {"cwd": str(work), "transport": mode}
            else:
                argv = ssh_argv(cfg)
                code, output, error = await run_process(
                    [*argv, "diagnose"], b"", cwd=str(root), env=env, timeout=20, output_limit=16384
                )
                if code:
                    raise CursorError(ssh_error_code(error))
                try:
                    report = json.loads(output)
                    capability = (
                        report.get("cleanup_protocol") if isinstance(report, dict) else None
                    )
                except (ValueError, UnicodeError, RecursionError):
                    raise CursorError("protocol") from None
                if type(capability) is not int or capability != RUNNER_CLEANUP_PROTOCOL:
                    raise CursorError("runner_cleanup_unsupported")
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(cwd),
                start_new_session=True,
                close_fds=True,
                limit=max(cfg["context_max_bytes"] * 2, cfg["output_max_bytes"]) + 65536,
            )
            if ready is not None:
                ready["transport_pid"] = self.process.pid
            record("process_started", transport=mode)
            return self.process, ready
        except CursorError as exc:
            record("process_start_failed", transport=mode, code=exc.code)
            await self.close()
            raise
        except OSError:
            record("process_start_failed", transport=mode, code="transport")
            await self.close()
            raise CursorError("transport") from None

    async def close(self):
        # A queue deadline or a shutdown may cancel the caller while the child
        # is still running. Keep one cleanup in flight and hold the login lock
        # until process shutdown has finished, even across repeated cancellation.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        closing = self._close_task
        try:
            await wait_for_cleanup(closing)
        finally:
            if self._close_task is closing:
                self._close_task = None

    async def _close(self):
        proc, self.process = self.process, None
        try:
            if proc is not None and self.kind == "ssh":
                await self._close_remote(proc)
            else:
                await self._close_local(proc)
        finally:
            self.sync_credentials()
            self.temporary_auth = None
            if self.lock is not None:
                self.lock.close()
                self.lock = None
            if self.scratch is not None:
                self.scratch.cleanup()
                self.scratch = None

    async def _close_remote(self, proc):
        async def discard(stream):
            while await stream.read(16384):
                pass

        if proc.stdin:
            # Do not wait for buffered ACP input to flush during shutdown.
            proc.stdin.transport.abort()
        try:
            code, _, _ = await asyncio.wait_for(
                asyncio.gather(proc.wait(), discard(proc.stdout), discard(proc.stderr)), timeout=10
            )
        except (asyncio.TimeoutError, OSError):
            code = None
        if code != RUNNER_CLEANUP_CONFIRMED:
            # Forcing the local SSH process down does not prove remote cleanup.
            try:
                await self._close_local(proc)
            except CursorError:
                pass
            record("process_cleanup_failed", transport="ssh", code="cleanup_unverified")
            raise CursorError("cleanup_unverified")
        record("process_cleanup_verified", transport="ssh", code="ok")

    async def _close_local(self, proc):
        if proc is not None:
            if proc.stdin:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # Drain pipes even if a descendant retained an FD. Never wait forever.
                    for stream in (proc.stdout, proc.stderr):
                        transport = getattr(stream, "_transport", None)
                        if transport is not None:
                            transport.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 2)
                    except asyncio.TimeoutError:
                        record(
                            "process_cleanup_failed", transport=self.kind, code="cleanup_timeout"
                        )
                        raise CursorError("cleanup_timeout") from None
            finally:
                if self.kind == "local_direct":
                    # Also stop children in the original process group if the
                    # CLI exited while they held no ACP pipe FDs.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def sync_credentials(self):
        if self.temporary_auth is not None:
            try:
                refreshed = read_browser_credentials(self.temporary_auth)
                current = local_credentials(self.cfg)
                if current == self.original_auth and refreshed != browser_tokens(current):
                    replacement = (
                        {**current, **refreshed} if self.kind == "local_direct" else refreshed
                    )
                    write_credentials(self.master_auth, replacement)
                    self.original_auth = replacement
            except (CursorError, OSError):
                record("credential_persist_failed", transport=self.kind, code="auth_persist")
