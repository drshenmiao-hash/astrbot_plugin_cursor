"""Shared bounded queue, persistent ACP runtime, and authoritative history reconciliation."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from .acp import ACPClient
from .errors import CursorError, ImageLimitError, retry_stream_errors
from .images import prompt_images
from .launcher import ProcessOwner, ssh_error_code
from .logic import history_state, reconcile_history
from .metrics import record
from .process import wait_for_cleanup
from .protocol import encode
from .queueing import RequestQueue
from .request_status import failure_stage, safe_transport
from .transcript import render_messages


@dataclass
class Job:
    messages: list
    scope: str | None
    request_id: str
    future: asyncio.Future
    enqueued: float
    deadline: float
    input_bytes: int
    target_model: str
    resolve_alias: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    sent_bytes: int = 0
    wire_bytes: int = 0
    reused: bool = False
    reset_reason: str = "new_conversation"
    output_bytes: int = 0
    queue_ms: int | None = None
    process_reused: bool = False
    accounted: bool = False
    sequence: int | None = None
    stats_session: str | None = None
    request_max_retries: int | None = 1
    model_id: str | None = None
    transport: str | None = None
    stage: str = "queue"
    delivery: str = "not_sent"
    images: dict | None = None
    sent_image_blocks: int = 0
    sent_image_base64_bytes: int = 0
    historical_images_not_attached: int = 0
    image_limit: dict = field(default_factory=dict)


class Engine:
    def __init__(self, cfg, *, owner_factory=ProcessOwner):
        self.cfg = cfg
        self.owner_factory = owner_factory
        self.references = 0
        self.queue = RequestQueue(cfg["queue_capacity"])
        self.workers = []
        self.sessions = {}
        self.client = None
        self.owner = None
        self.active = 0
        self.session_slots = 0
        self.closed = False
        self._close_task = None
        self._condition = asyncio.Condition()
        self._start_lock = asyncio.Lock()
        self._jobs = {}
        self.last_failure = None
        self._runner_busy_until = 0.0

    async def _start_workers(self):
        async with self._start_lock:
            if self.closed:
                raise CursorError("closed")
            if not self.workers:
                self.workers = [
                    asyncio.create_task(self._worker(), name=f"cursor-worker-{i}")
                    for i in range(self.cfg["max_concurrency"])
                ]

    async def submit(
        self,
        messages,
        scope,
        *,
        on_queued=None,
        sequence=None,
        stats_session=None,
        images=None,
        request_max_retries=1,
        target_model=None,
        resolve_alias=False,
    ):
        await self._start_workers()
        now = time.monotonic()
        size = len(encode(messages))
        if size > self.cfg["context_max_bytes"]:
            raise CursorError("context_limit")
        job = Job(
            messages,
            scope,
            uuid.uuid4().hex,
            asyncio.get_running_loop().create_future(),
            now,
            now + self.cfg["queue_timeout_seconds"],
            size,
            self.cfg["model"] if target_model is None else target_model,
            resolve_alias,
        )
        job.sequence = sequence if type(sequence) is int else None
        job.stats_session = stats_session
        job.images = images
        job.request_max_retries = request_max_retries
        self._jobs[job.request_id] = job

        async def queue_notice():
            if on_queued is None:
                return
            try:
                await asyncio.wait_for(job.started.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    await on_queued(self.queue.qsize())
                except Exception:
                    record("queue_notice_failed", request_id=job.request_id, code="notification")

        notice = asyncio.create_task(queue_notice())
        try:
            await asyncio.wait_for(self.queue.put(job), max(0.001, job.deadline - time.monotonic()))
            if not job.cancel.is_set():
                record("queued", request_id=job.request_id, queue_depth=self.queue.qsize())
            await asyncio.wait_for(job.started.wait(), max(0.001, job.deadline - time.monotonic()))
            return await job.future
        except asyncio.TimeoutError:
            job.cancel.set()
            if not job.future.done():
                job.future.cancel()
            record("queue_expired", request_id=job.request_id, code="queue_timeout")
            self._account(job, "queue_timeout")
            raise CursorError("queue_timeout") from None
        except asyncio.CancelledError:
            job.cancel.set()
            if not job.future.done():
                job.future.cancel()
            if not job.started.is_set():
                self._account(job, "cancelled")
            raise
        finally:
            notice.cancel()
            if job.cancel.is_set():
                await self.queue.discard(job)
            await asyncio.gather(notice, return_exceptions=True)
            self._jobs.pop(job.request_id, None)

    def _reuse(self, job):
        cached = self.sessions.get(job.scope)
        if cached and (job.target_model, job.resolve_alias) != cached["target"]:
            cached = None
        plan = reconcile_history(
            cached["history"] if cached else None,
            job.messages,
            last_request_hashes=cached["request_hashes"] if cached else None,
        )
        if cached and plan["reused"]:
            return cached, plan
        return None, plan

    def _no_new_input(self, job, cached):
        # Do not send a system-only prompt or acknowledge pending system changes.
        # They are diffed against the last successfully applied state next turn.
        reply = cached["last_reply"] if cached else ""
        job.reused = cached is not None
        job.reset_reason = "cached_reply" if cached else "no_new_user_input"
        job.output_bytes = len(reply.encode())
        return reply

    async def _new_client(self, *, startup_timeout=None):
        owner = self.owner_factory(self.cfg)
        client = None
        try:
            async with asyncio.timeout(startup_timeout):
                process, ready = await owner.start()
                client = ACPClient(
                    process,
                    frame_limit=max(self.cfg["context_max_bytes"] * 2, self.cfg["output_max_bytes"])
                    + 65536,
                    output_limit=self.cfg["output_max_bytes"],
                    ready=ready,
                )
                await client.start()
            self.owner, self.client = owner, client
        except BaseException as exc:
            code = (
                exc.code
                if isinstance(exc, CursorError)
                else "startup_timeout"
                if isinstance(exc, TimeoutError)
                else "cli_failed"
            )
            try:
                if (
                    client is not None
                    and isinstance(exc, CursorError)
                    and exc.code == "process_exited"
                ):
                    try:
                        await asyncio.wait_for(asyncio.shield(client._stderr_task), 1)
                    except asyncio.TimeoutError:
                        pass
                    if owner.kind == "ssh" and client.startup_stage == "runner":
                        code = ssh_error_code(bytes(client.stderr_sample))
                    elif b"bwrap" in client.stderr_sample:
                        code = "sandbox_unavailable"
            finally:
                await self._close_transport(client, owner)
            stage = failure_stage(code, client.startup_stage if client is not None else "runner")
            if isinstance(exc, Exception):
                record("process_start_failed", transport=owner.kind, code=code, stage=stage)
                error = CursorError(code)
                error.stage, error.transport = stage, safe_transport(owner.kind)
                error.handshake_complete = bool(client and client.handshake_complete)
                raise error from None
            raise

    @staticmethod
    async def _close_transport(client, owner):
        async def close():
            try:
                if client is not None:
                    await client.close()
            finally:
                if owner is not None:
                    try:
                        await owner.close()
                    except CursorError as cleanup:
                        cleanup.stage, cleanup.transport = "runner", safe_transport(owner.kind)
                        raise

        if client is not None or owner is not None:
            await wait_for_cleanup(asyncio.create_task(close()))

    async def _reset_client(self, reason):
        client, owner = self.client, self.owner
        self.client, self.owner = None, None
        self.sessions.clear()
        self.session_slots = 0
        await self._close_transport(client, owner)
        record("runtime_reset", reset_reason=reason)

    async def available_models(self, *, reuse_only=False):
        """One ACP session/new catalog query; never send a model prompt."""
        async with self._condition:
            if self.closed:
                raise CursorError("closed")
            if reuse_only and (
                self.client is None
                or self.client.closed
                or self.session_slots >= self.cfg["max_sessions"]
            ):
                raise CursorError("runner_in_use")
            rotate = self.client is not None and (
                self.client.closed or self.session_slots >= self.cfg["max_sessions"]
            )
            if rotate:
                while self.active:
                    await self._condition.wait()
                await self._reset_client("catalog_refresh")
            if self.client is None:
                await self._new_client()
            self.session_slots += 1
            return await self.client.available_models()

    async def _lease(self, job):
        async with self._condition:
            while True:
                if self.closed or job.cancel.is_set():
                    raise CursorError("closed" if self.closed else "cancelled")
                cached, _ = self._reuse(job)
                exhausted = cached is None and self.session_slots >= self.cfg["max_sessions"]
                dead = self.client is not None and self.client.closed
                if (exhausted or dead) and self.active:
                    remaining = job.deadline - time.monotonic()
                    if remaining <= 0:
                        raise CursorError("queue_timeout")
                    await asyncio.wait_for(self._condition.wait(), remaining)
                    continue
                job.process_reused = self.client is not None and not self.client.closed
                if exhausted or dead:
                    await self._reset_client("session_limit" if exhausted else "process_exited")
                    job.process_reused = False
                if self.client is None:
                    job.stage = "runner"
                    await self._start_chat_client(job.deadline)
                job.transport = safe_transport(
                    self.owner.kind
                    if self.owner
                    else getattr(self.client, "ready", {}).get("transport")
                )
                cached, _ = self._reuse(job)
                if cached is None:
                    self.session_slots += 1
                self.active += 1
                return self.client

    async def _start_chat_client(self, deadline):
        # All chat workers enter through _lease's condition. Share a short cooldown
        # after an unsuccessful batch instead of opening SSH again for every waiter.
        if time.monotonic() < self._runner_busy_until:
            raise CursorError("runner_in_use")
        for delay in (0.25, 0.75, None):
            try:
                await self._new_client()
                return
            except CursorError as exc:
                if exc.code != "runner_in_use":
                    raise
                now = time.monotonic()
                if delay is None or now + delay >= deadline:
                    self._runner_busy_until = now + 2.0
                    raise
                await asyncio.sleep(delay)

    async def _release(self):
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()

    async def _cancel_turn(self, client, session_id, turn):
        try:
            await asyncio.wait_for(client.notify("session/cancel", {"sessionId": session_id}), 5)
            try:
                await asyncio.wait_for(asyncio.shield(turn), 5)
            except CursorError as exc:
                if exc.code != "cancelled":
                    raise
            record("cancel_confirmed")
        except (CursorError, asyncio.TimeoutError, OSError):
            # No acknowledgement: stop the transport/runtime rather than reusing
            # an unknown session. Other in-flight turns fail explicitly too.
            await self._close_transport(client, self.owner)
            record("cancel_unconfirmed", code="cancel_unconfirmed")
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    async def _execute(self, job, client):
        cached, plan = self._reuse(job)
        previous = self.sessions.get(job.scope)
        target = (job.target_model, job.resolve_alias)
        if previous and previous["target"] == target:
            record(
                "history_comparison",
                request_id=job.request_id,
                session=job.stats_session or job.scope,
                **{
                    key: plan[key]
                    for key in ("length_rule", "hash_rule", "tail_rule", "system_changed")
                },
                **plan["history_difference"],
            )
        if not plan["has_new_user_input"]:
            return self._no_new_input(job, cached)
        delta = plan["delta"]
        job.reused = cached is not None
        job.reset_reason = (
            ("system_updated" if plan["system_changed"] else "continuous_history")
            if cached
            else ("history_changed" if previous else "new_conversation")
        )
        if previous and previous["target"] != target:
            job.reset_reason = "model_changed"
        if not job.scope:
            job.reset_reason = "no_stable_session_key"
        job.stage = "model"
        session_id = (
            cached["session_id"]
            if cached
            else await client.new_session(
                job.target_model, **({"resolve_alias": True} if job.resolve_alias else {})
            )
        )
        delta, image_blocks = prompt_images(delta, job.images or {})
        job.historical_images_not_attached = sum(
            entry.get("historical_images_not_attached", 0) for entry in delta
        )
        text = render_messages(
            delta,
            incremental=bool(cached),
            clock_fields=plan["clock_fields"],
            image_count=len(image_blocks),
        )
        job.sent_bytes = len(text.encode())
        stats = {}
        turn = asyncio.create_task(
            client.prompt(
                session_id, text, stats=stats, **({"images": image_blocks} if image_blocks else {})
            )
        )
        try:
            completed = await asyncio.shield(turn)
            reply = completed["text"]
            job.wire_bytes = completed["wire_bytes"]
            job.output_bytes = len(reply.encode())
            if job.scope:
                self.sessions[job.scope] = {
                    "session_id": session_id,
                    "target": target,
                    "history": history_state(
                        job.messages + [{"role": "assistant", "content": reply}]
                    ),
                    "request_hashes": history_state(job.messages)["request_hashes"],
                    "last_reply": reply,
                }
            if self.owner is not None:
                self.owner.sync_credentials()
            return reply
        except asyncio.CancelledError:
            if job.scope:
                self.sessions.pop(job.scope, None)
            await self._cancel_turn(client, session_id, turn)
            raise
        except BaseException:
            if job.scope:
                self.sessions.pop(job.scope, None)
            if not turn.done():
                await self._cancel_turn(client, session_id, turn)
            raise
        finally:
            if turn.done():
                await asyncio.gather(turn, return_exceptions=True)
            job.wire_bytes = stats.get("wire_bytes", job.wire_bytes)
            job.output_bytes = stats.get("output_bytes", job.output_bytes)
            job.model_id = stats.get("model_id")
            job.delivery = stats.get("delivery", "sent" if job.wire_bytes else "not_sent")
            if job.wire_bytes:
                job.sent_image_blocks = len(image_blocks)
                job.sent_image_base64_bytes = sum(len(block["data"]) for block in image_blocks)

    async def _run_attempt(self, job):
        leased = False
        operation = cancellation = None
        code = "ok"
        try:
            if job.cancel.is_set() or job.future.cancelled():
                raise CursorError("cancelled")
            cached, plan = self._reuse(job)
            if not plan["has_new_user_input"]:
                job.queue_ms = round((time.monotonic() - job.enqueued) * 1000)
                job.process_reused = bool(self.client and not self.client.closed)
                job.started.set()
                return self._no_new_input(job, cached)
            client = await asyncio.wait_for(
                self._lease(job), max(0.001, job.deadline - time.monotonic())
            )
            leased = True
            if job.cancel.is_set() or job.future.cancelled():
                raise CursorError("cancelled")
            job.queue_ms = round((time.monotonic() - job.enqueued) * 1000)
            job.started.set()
            operation = asyncio.create_task(self._execute(job, client))
            cancellation = asyncio.create_task(job.cancel.wait())
            done, _ = await asyncio.wait(
                {operation, cancellation},
                timeout=self.cfg["timeout_seconds"],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            code = "cancelled" if job.cancel.is_set() else "timeout"
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise CursorError(code)
        except asyncio.TimeoutError:
            code = "queue_timeout"
            raise CursorError(code) from None
        except CursorError as exc:
            code = exc.code
            job.stage = exc.stage or job.stage
            job.transport = exc.transport or job.transport
            if isinstance(exc, ImageLimitError):
                job.image_limit = exc.metadata
            raise
        except asyncio.CancelledError:
            code = "closed"
            if operation is not None:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception:
            code = "cli_failed"
            raise CursorError(code) from None
        finally:
            # Wake the caller even when startup failed before a prompt began.
            job.started.set()
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
            if leased:
                await self._release()
            self._account(job, code)

    async def _worker(self):
        while True:
            job = await self.queue.get()
            current = None

            async def attempt():
                nonlocal current
                if current is None:
                    current = job
                else:
                    # Keep the reservation and the original caller. Only per-attempt
                    # accounting and the deadline for acquiring a runtime start afresh.
                    now = time.monotonic()
                    current = Job(
                        job.messages,
                        job.scope,
                        uuid.uuid4().hex,
                        job.future,
                        now,
                        now + self.cfg["queue_timeout_seconds"],
                        job.input_bytes,
                        job.target_model,
                        job.resolve_alias,
                        started=job.started,
                        cancel=job.cancel,
                        sequence=job.sequence,
                        stats_session=job.stats_session,
                        images=job.images,
                    )
                return await self._run_attempt(current)

            try:
                result = await retry_stream_errors(
                    attempt, request_max_retries=job.request_max_retries
                )
            except CursorError as exc:
                if not job.future.done():
                    job.future.set_exception(exc.with_traceback(None))
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.set_exception(CursorError("closed"))
                raise
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                # Another turn in this conversation may start only after all retries.
                await self.queue.task_done(job)

    def _account(self, job, code):
        if job.accounted:
            return
        job.accounted = True
        if code not in {"ok", "cancelled", "closed"}:
            self.last_failure = code
        record(
            "request_finished",
            request_id=job.request_id,
            session=job.stats_session or job.scope,
            model_id=job.model_id,
            transport=job.transport,
            stage=failure_stage(code, job.stage, sent=job.delivery != "not_sent"),
            delivery=job.delivery,
            code=code,
            input_bytes=job.input_bytes,
            sent_input_bytes=job.sent_bytes,
            wire_bytes=job.wire_bytes,
            output_bytes=job.output_bytes,
            queue_ms=job.queue_ms
            if job.queue_ms is not None
            else round((time.monotonic() - job.enqueued) * 1000),
            duration_ms=round((time.monotonic() - job.enqueued) * 1000),
            reused=job.reused,
            process_reused=job.process_reused,
            sequence=job.sequence,
            reset_reason=job.reset_reason,
            active=self.active,
            queue_depth=self.queue.qsize(),
            sent_image_blocks=job.sent_image_blocks,
            sent_image_base64_bytes=job.sent_image_base64_bytes,
            historical_images_not_attached=job.historical_images_not_attached
            if job.wire_bytes
            else 0,
            **job.image_limit,
        )

    def has_pending_requests(self):
        return bool(self._jobs)

    def health(self):
        return {
            "process_alive": bool(self.client and not self.client.closed),
            "active": self.active,
            "queue_depth": self.queue.qsize(),
            "cached_sessions": len(self.sessions),
            "max_concurrency": self.cfg["max_concurrency"],
            "last_failure": self.last_failure,
        }

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._shutdown())
        await wait_for_cleanup(self._close_task)

    async def _shutdown(self):
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
            job.started.set()
            if not job.future.done():
                job.future.set_exception(CursorError("closed"))
        for worker in self.workers:
            worker.cancel()
        await self.queue.clear()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        for job in jobs:
            self._account(job, "closed")
        await self._reset_client("shutdown")
