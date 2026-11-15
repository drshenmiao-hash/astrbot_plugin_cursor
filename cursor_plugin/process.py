"""Bounded asynchronous subprocess IO, without a shell or inherited env."""

import asyncio
import os
import signal

from .errors import CursorError


async def run_process(argv, payload, *, cwd, env, timeout, output_limit):
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
            limit=65536,
        )
    except OSError:
        raise CursorError("transport") from None

    async def read_bounded(stream, limit):
        chunks = []
        size = 0
        while chunk := await stream.read(16384):
            size += len(chunk)
            if size > limit:
                raise CursorError("output_limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def write_input():
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    jobs = [
        asyncio.create_task(read_bounded(proc.stdout, output_limit)),
        asyncio.create_task(read_bounded(proc.stderr, 65536)),
        asyncio.create_task(write_input()),
        asyncio.create_task(proc.wait()),
    ]
    try:
        result = await asyncio.wait_for(asyncio.gather(*jobs), timeout=timeout)
        return result[3], result[0], result[1]
    except asyncio.TimeoutError:
        raise CursorError("timeout") from None
    finally:
        # Kill the entire session, including descendants retaining stdout/stderr.
        # This also runs on cancellation, output overflow and plugin unload.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for job in jobs:
            if not job.done():
                job.cancel()
        for stream in (proc.stdout, proc.stderr):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()
        await asyncio.gather(*jobs, return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            # A broken transport must not make diagnostics hang indefinitely.
            pass
