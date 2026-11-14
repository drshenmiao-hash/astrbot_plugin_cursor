"""Run inside the dedicated container only. No AstrBot imports or HTTP server."""

import asyncio
import fcntl
import json
import os
import signal
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import CursorError
from .process import run_process
from .protocol import MAX_CONTEXT, MAX_INPUT, MAX_OUTPUT, VERSION, decode, encode, explicit_model

AGENT = "/opt/cursor/cursor-agent"
AUTH = Path("/var/lib/cursor-login/.config/cursor/auth.json")
LOCK = "/var/lib/cursor-login/.runner.lock"
DENY = ["Shell(*)", "Read(**)", "Read(/**)", "Write(**)", "Write(/**)", "WebFetch(*)", "Mcp(*:*)"]


def cli_env(home):
    home = str(home)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "NO_OPEN_BROWSER": "1",
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_CACHE_HOME": home + "/.cache",
        "XDG_DATA_HOME": home + "/.local/share",
        "CURSOR_CONFIG_DIR": home + "/.config/cursor",
        "CURSOR_DATA_DIR": home + "/.cursor",
        "AGENT_CLI_CREDENTIAL_STORE": "file",
    }


def read_browser_credentials(path):
    # Never accept API keys or unrelated provider credentials as a substitute.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise CursorError("config")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise CursorError("config")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CursorError("config")
        if value.get("apiKey") or value.get("bedrockCredentials"):
            raise CursorError("config")
        if not isinstance(value.get("accessToken"), str) or not value["accessToken"]:
            raise CursorError("not_logged_in")
        result = {"accessToken": value["accessToken"]}
        if isinstance(value.get("refreshToken"), str):
            result["refreshToken"] = value["refreshToken"]
        return result
    except FileNotFoundError:
        raise CursorError("not_logged_in") from None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CursorError("config") from None


def write_credentials(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_name(".auth-pending.json")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(encode(value))
    os.chmod(pending, 0o600)
    os.replace(pending, path)


def cli_reply(code, out, err):
    if code:
        # Inspect privately; never interpolate this text into any error/log.
        error = (err + out).decode("utf-8", errors="replace").lower()
        if any(
            marker in error
            for marker in (
                "authentication required",
                "not authenticated",
                "not logged in",
                "unauthenticated",
                "please log in",
                "please login",
                "token expired",
                "invalid access token",
                "token has expired",
            )
        ):
            raise CursorError("not_logged_in")
        raise CursorError("cli_failed")
    try:
        value = json.loads(out)
    except (ValueError, UnicodeError, RecursionError):
        raise CursorError("protocol") from None
    if (
        not isinstance(value, dict)
        or value.get("type") != "result"
        or value.get("subtype") != "success"
        or value.get("is_error") is not False
        or not isinstance(value.get("result"), str)
        or not value["result"].strip()
    ):
        raise CursorError("protocol")
    return value["result"]


async def complete(request, *, agent=AGENT, auth_path=AUTH, allowed_model=None):
    # agent/auth overrides are Python-only seams for the supplied mock tests;
    # request data cannot choose an executable, path, env or command flags.
    model = explicit_model(request.get("model"))
    if model != explicit_model(allowed_model):
        raise CursorError("model")
    prompt = request.get("prompt")
    timeout = request.get("timeout")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CursorError("protocol")
    if len(prompt.encode("utf-8")) > MAX_CONTEXT:
        raise CursorError("context_limit")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise CursorError("config")
    credentials = read_browser_credentials(auth_path)
    with TemporaryDirectory(prefix="cursor-call-") as scratch:
        root = Path(scratch)
        work = root / "work"
        home = root / "home"
        work.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        temporary_auth = home / ".config/cursor/auth.json"
        write_credentials(temporary_auth, credentials)
        config = {
            "version": 1,
            "editor": {"vimMode": False},
            "approvalMode": "allowlist",
            "permissions": {"allow": [], "deny": DENY},
            "maxMode": False,
        }
        (temporary_auth.parent / "cli-config.json").write_bytes(encode(config))
        argv = [
            agent,
            "--print",
            "--mode",
            "ask",
            "--model",
            model,
            "--output-format",
            "json",
            "--trust",
        ]
        try:
            code, out, err = await run_process(
                argv,
                prompt.encode("utf-8"),
                cwd=str(work),
                env=cli_env(home),
                timeout=timeout,
                output_limit=MAX_OUTPUT - 65536,
            )
            return cli_reply(code, out, err)
        finally:
            # Preserve a refreshed browser token, never CLI chat/config/log data.
            # The caller holds the container-wide lock through this operation.
            try:
                refreshed = read_browser_credentials(temporary_auth)
            except CursorError:
                refreshed = credentials
            if refreshed != credentials:
                write_credentials(auth_path, refreshed)


async def serve():
    reader = asyncio.StreamReader(limit=MAX_INPUT + 1)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    pipe, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(0, "rb", closefd=False))
    try:
        try:
            raw = await asyncio.wait_for(reader.read(MAX_INPUT + 1), 10)
            # read(n) can return early on a pipe: consume until EOF, bounded.
            chunks = [raw]
            size = len(raw)

            async def read_rest():
                nonlocal size
                while chunk := await reader.read(min(16384, MAX_INPUT + 1 - size)):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_INPUT:
                        raise CursorError("context_limit")

            if size > MAX_INPUT:
                raise CursorError("context_limit")
            await asyncio.wait_for(read_rest(), 10)
            request = decode(b"".join(chunks))
        except asyncio.TimeoutError:
            raise CursorError("timeout") from None
        # Serial execution prevents one CLI from seeing another QQ session's
        # temporary files, and avoids racing browser-token refreshes.
        with open(LOCK, "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CursorError("busy") from None
            allowed = Path("/etc/cursor-model-id").read_text(encoding="utf-8").strip()
            text = await complete(request, allowed_model=allowed)
        return {"version": VERSION, "ok": True, "text": text}
    finally:
        pipe.close()


def main():
    os.umask(0o077)

    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await serve()
        except CursorError as exc:
            return {"version": VERSION, "ok": False, "code": exc.code}
        except asyncio.CancelledError:
            return {"version": VERSION, "ok": False, "code": "closed"}
        except Exception:
            return {"version": VERSION, "ok": False, "code": "cli_failed"}

    # Even failures use the envelope and exit 0. Nonzero SSH exits are transport
    # failures. There are intentionally no logging calls or raw tracebacks.
    try:
        os.write(1, encode(asyncio.run(run())) + b"\n")
    except (BrokenPipeError, ConnectionResetError):
        pass


if __name__ == "__main__":
    main()
