"""Bounded ACP v1 stdio client. One reader dispatches concurrent session replies."""

import asyncio
import json
import time

from .errors import CursorError, CursorStreamError, ImageLimitError
from .images import MAX_IMAGE_FRAME_BYTES
from .logic import (
    classify_chunk_tail_error,
    classify_error,
    classify_image_error,
    classify_reply_error,
)
from .models import ModelResolutionError, catalog_ids, resolve_model


class ACPClient:
    def __init__(self, process, *, frame_limit, output_limit, ready=None):
        self.process = process
        self.frame_limit = frame_limit
        self.output_limit = output_limit
        self.ready = ready or {"cwd": "/work", "transport": "local"}
        self.initialized = None
        self.startup_stage = "runner"
        self.handshake_complete = False
        self.authenticated = False
        self.pending = {}
        self.turns = {}
        self.session_models = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        self.closed = False
        self.failure_code = None
        self.stderr_sample = bytearray()
        self.last_activity = time.monotonic()
        self._ready_future = asyncio.get_running_loop().create_future()
        if ready is not None:
            self._ready_future.set_result(ready)

    async def _discard_stderr(self):
        # Retain at most 8 KiB privately for dependency error classification.
        # This buffer is never logged, returned to chat, or written to disk.
        while chunk := await self.process.stderr.read(16384):
            if len(self.stderr_sample) < 8192:
                self.stderr_sample.extend(chunk[: 8192 - len(self.stderr_sample)])

    async def _write(self, message, *, image_prompt=False, prompt_state=None):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        limit = self.frame_limit + (MAX_IMAGE_FRAME_BYTES if image_prompt else 0)
        if len(raw) > limit:
            if image_prompt:
                raise ImageLimitError("frame_bytes", limit, len(raw))
            raise CursorError("context_limit")
        async with self._write_lock:
            try:
                self.process.stdin.write(raw)
                if prompt_state is not None:
                    prompt_state["write_started"] = True
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise CursorError("process_exited") from None
        return len(raw)

    async def call(self, method, params, *, timeout=None, image_prompt=False):
        if self.closed:
            raise CursorError(self.failure_code or "process_exited")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            prompt_state = (
                self.turns.get(params.get("sessionId")) if method == "session/prompt" else None
            )
            sent_bytes = await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                image_prompt=image_prompt,
                **({"prompt_state": prompt_state} if prompt_state is not None else {}),
            )
            if prompt_state is not None:
                prompt_state["wire_bytes"] = sent_bytes
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise CursorError("startup_timeout") from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method, params):
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def start(self):
        try:
            await asyncio.wait_for(asyncio.shield(self._ready_future), 20)
            self.startup_stage = "handshake"
            self.initialized = await self.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                        "_meta": {"parameterizedModelPicker": True},
                    },
                    "clientInfo": {"name": "astrbot_plugin_cursor", "version": "0.4.3"},
                },
                timeout=30,
            )
            if self.initialized.get("protocolVersion") != 1:
                raise CursorError("protocol")
            self.handshake_complete = True
            self.startup_stage = "auth"
            await self.call("authenticate", {"methodId": "cursor_login"}, timeout=30)
            self.authenticated = True
        except asyncio.TimeoutError:
            raise CursorError("startup_timeout") from None

    async def _open_session(self):
        result = await self.call(
            "session/new", {"cwd": self.ready["cwd"], "mcpServers": []}, timeout=30
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise CursorError("protocol")
        model_info = result.get("models")
        if not isinstance(model_info, dict):
            raise CursorError("protocol")
        models = model_info.get("availableModels")
        catalog_ids(models)
        return result

    async def available_models(self):
        result = await self._open_session()
        return result["models"]["availableModels"]

    async def new_session(self, model):
        result = await self._open_session()
        session_id = result["sessionId"]
        try:
            model = resolve_model(model, result["models"]["availableModels"])
        except ModelResolutionError as exc:
            # The standalone ACP checks run without AstrBot installed.
            try:
                from astrbot.api import logger
            except ModuleNotFoundError as missing:
                if missing.name != "astrbot":
                    raise
            else:
                logger.warning("%s", exc)
            raise
        if not any(
            m.get("id") == "ask"
            for m in result.get("modes", {}).get("availableModes", [])
            if isinstance(m, dict)
        ):
            raise CursorError("ask_unavailable")
        selected = await self.call(
            "session/set_config_option",
            {
                "sessionId": session_id,
                "configId": "model",
                "value": model,
            },
            timeout=30,
        )
        options = selected.get("configOptions", [])
        if not any(
            o.get("id") == "model" and o.get("currentValue") == model
            for o in options
            if isinstance(o, dict)
        ):
            raise CursorError("model_mismatch")
        await self.call("session/set_mode", {"sessionId": session_id, "modeId": "ask"}, timeout=30)
        self.session_models[session_id] = model
        return session_id

    async def prompt(self, session_id, text, *, stats=None, images=()):
        if images:
            capabilities = (self.initialized or {}).get("agentCapabilities")
            supported = (
                capabilities.get("promptCapabilities") if isinstance(capabilities, dict) else None
            )
            if not isinstance(supported, dict) or supported.get("image") is not True:
                raise CursorError("image_unsupported")
        state = {"parts": [], "size": 0, "error": None}
        self.turns[session_id] = state
        try:
            result = await self.call(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}, *images],
                },
                **({"image_prompt": True} if images else {}),
            )
            if state["error"]:
                raise CursorError(state["error"])
            if result.get("stopReason") == "cancelled":
                raise CursorError("cancelled")
            if result.get("stopReason") not in {
                "end_turn",
                "max_tokens",
                "max_turn_requests",
                "refusal",
            }:
                raise CursorError("cli_failed")
            text = "".join(state["parts"])
            if not text.strip():
                raise CursorError("empty_output")
            if images:
                image_error = classify_image_error(text, reply=True) or classify_image_error(
                    state["parts"][-1], reply=True
                )
                if image_error:
                    raise CursorError(image_error)
            error_code = classify_reply_error(text) or classify_chunk_tail_error(
                text, state["parts"]
            )
            if error_code:
                raise CursorStreamError(error_code)
            return {"text": text, "wire_bytes": state.get("wire_bytes", 0)}
        except CursorStreamError:
            raise
        except CursorError as exc:
            if exc.code in {"connection_stalled", "ping_timeout", "upstream_retryable"}:
                raise CursorStreamError(exc.code) from None
            raise
        finally:
            if stats is not None:
                stats.update(wire_bytes=state.get("wire_bytes", 0), output_bytes=state["size"])
                stats["delivery"] = (
                    "sent"
                    if state.get("wire_bytes")
                    else "unconfirmed"
                    if state.get("write_started")
                    else "not_sent"
                )
                stats["model_id"] = (
                    self.session_models.get(session_id) if stats["delivery"] != "not_sent" else None
                )
            self.turns.pop(session_id, None)
            self.last_activity = time.monotonic()

    async def _server_request(self, message):
        method = message.get("method")
        params = message.get("params", {})
        if method == "session/request_permission":
            rejected = next(
                (
                    o.get("optionId")
                    for o in params.get("options", [])
                    if o.get("kind") == "reject_once"
                ),
                None,
            )
            result = (
                {"outcome": {"outcome": "selected", "optionId": rejected}}
                if rejected
                else {"outcome": {"outcome": "cancelled"}}
            )
        elif method == "cursor/ask_question":
            result = {
                "outcome": {
                    "outcome": "skipped",
                    "reason": "Interactive questions are unavailable in this provider.",
                }
            }
        elif method == "cursor/create_plan":
            result = {"outcome": {"outcome": "rejected", "reason": "Ask mode is fixed."}}
        else:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Client capability unavailable"},
                }
            )
            return
        await self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _update(self, params):
        state = self.turns.get(params.get("sessionId"))
        if state is None:
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") != "text" or not isinstance(content.get("text"), str):
                state["error"] = "unsupported_output"
                return not state.get("cancel_sent", False)
            text = content["text"]
            state["size"] += len(text.encode())
            if state["size"] > self.output_limit:
                state["error"] = "output_limit"
                return not state.get("cancel_sent", False)
            state["parts"].append(text)
        elif kind == "current_mode_update" and update.get("currentModeId") != "ask":
            state["error"] = "ask_unavailable"
        # Cursor's internal tool events are not AstrBot function-call results.
        # Permission requests are explicitly rejected by _server_request.
        return bool(state["error"]) and not state.get("cancel_sent", False)

    async def _read_loop(self):
        failure = "process_exited"
        try:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    break
                if len(raw) > self.frame_limit:
                    raise CursorError("output_limit")
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise CursorError("protocol")
                method = message.get("method")
                if method == "astrbot/cursor_ready":
                    params = message.get("params", {})
                    if not isinstance(params.get("cwd"), str):
                        raise CursorError("protocol")
                    self.ready = params
                    if not self._ready_future.done():
                        self._ready_future.set_result(params)
                elif method == "astrbot/cursor_error":
                    raise CursorError(message.get("params", {}).get("code", "cli_failed"))
                elif method == "session/update":
                    params = message.get("params", {})
                    if self._update(params):
                        self.turns[params["sessionId"]]["cancel_sent"] = True
                        await self.notify("session/cancel", {"sessionId": params["sessionId"]})
                elif method and "id" in message:
                    await self._server_request(message)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        # ACP -32000 means authentication required. Never expose
                        # raw RPC error data, which can include prompts or URLs.
                        if error.get("code") == -32000:
                            code = "not_logged_in"
                        else:
                            message_text = str(error.get("message", ""))
                            code = classify_error(message_text)
                            if code in {"cli_failed", "timeout"}:
                                code = classify_reply_error(message_text) or code
                        future.set_exception(CursorError(code))
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    else:
                        future.set_exception(CursorError("protocol"))
        except asyncio.CancelledError:
            return
        except CursorError as exc:
            failure = exc.code
        except (ValueError, UnicodeError, RecursionError, TypeError, AttributeError):
            failure = "protocol"
        finally:
            self.closed = True
            self.failure_code = failure
            if not self._ready_future.done():
                self._ready_future.set_exception(CursorError(failure))
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CursorError(failure))

    async def close(self):
        self.closed = True
        if not self._ready_future.done():
            self._ready_future.cancel()
        for task in (self._reader_task, self._stderr_task):
            task.cancel()
        await asyncio.gather(self._reader_task, self._stderr_task, return_exceptions=True)
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(CursorError("closed"))
        # The owner closes/kills the process and persists refreshed credentials.
