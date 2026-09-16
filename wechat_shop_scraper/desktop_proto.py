from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import zlib
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Callable


DEBUG_PORT = 9421


class CdpCancelledError(RuntimeError):
    """The calling collection run was cancelled; never try another context."""


class CdpTimeoutError(TimeoutError):
    """A timed-out page command, with its identity preserved for recovery."""


def _put_varint(value: int) -> bytes:
    value = int(value)
    if value < 0:
        raise ValueError("varint must be non-negative")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _put_bytes(number: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _put_varint((number << 3) | 2) + _put_varint(len(value)) + value


def _put_uint(number: int, value: int) -> bytes:
    return _put_varint(number << 3) + _put_varint(value)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        current = data[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, offset
        shift += 7
    raise ValueError("invalid protobuf varint")


def _decode_fields(data: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value = data[offset:end]
            offset = end
        elif wire == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64")
            value = data[offset:end]
            offset = end
        elif wire == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _last(fields: dict[int, list[int | bytes]], number: int, default: Any) -> Any:
    values = fields.get(number)
    return values[-1] if values else default


def _text(value: int | bytes | object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _maybe_inflate(value: bytes) -> bytes:
    if len(value) < 2:
        return value
    try:
        return zlib.decompress(value)
    except zlib.error:
        return value


def _encode_chrome_devtools(op_id: int, payload: str, context: str = "") -> bytes:
    body = _put_uint(1, op_id) + _put_bytes(2, payload)
    if context:
        body += _put_bytes(3, context)
    return body


def _encode_debug_message(seq: int, category: str, body: bytes) -> bytes:
    return (
        _put_uint(1, seq)
        + _put_bytes(3, category)
        + _put_bytes(4, body)
        + _put_uint(5, 0)
        + _put_uint(6, 0)
    )


def decode_debug_message(data: bytes) -> dict[str, Any]:
    outer = _decode_fields(data)
    category = _text(_last(outer, 3, b""))
    raw_body = _last(outer, 4, b"")
    body = _maybe_inflate(raw_body if isinstance(raw_body, bytes) else b"")
    fields = _decode_fields(body) if body else {}
    decoded: dict[str, Any] = {}
    if category in {"chromeDevtools", "chromeDevtoolsResult"}:
        payload = _last(fields, 2, b"")
        if isinstance(payload, bytes):
            payload = _maybe_inflate(payload)
        decoded = {
            "op_id": int(_last(fields, 1, 0)),
            "payload": _text(payload),
            "jscontext_id": _text(_last(fields, 3, b"")),
        }
    elif category == "ping":
        decoded = {
            "ping_id": int(_last(fields, 1, 0)),
            "payload": _text(_last(fields, 2, b"")),
        }
    elif category in {"connectJsContext", "addJsContext", "removeJsContext"}:
        decoded = {
            "jscontext_id": _text(_last(fields, 1, b"")),
            "jscontext_name": _text(_last(fields, 2, b"")),
        }
    return {"category": category, "data": decoded}


def wrap_cdp_payload(payload: str, seq: int, op_id: int) -> bytes:
    return _encode_debug_message(
        seq, "chromeDevtools", _encode_chrome_devtools(op_id, payload)
    )


def wrap_cdp_context_payload(
    payload: str, seq: int, op_id: int, jscontext_id: str
) -> bytes:
    return _encode_debug_message(
        seq,
        "chromeDevtools",
        _encode_chrome_devtools(op_id, payload, str(jscontext_id)),
    )


def wrap_connect_js_context(seq: int, jscontext_id: str) -> bytes:
    return _encode_debug_message(
        seq, "connectJsContext", _put_bytes(1, str(jscontext_id))
    )


def wrap_pong(seq: int, ping_id: int, payload: str = "") -> bytes:
    body = _put_uint(1, ping_id)
    if payload:
        body += _put_bytes(2, payload)
    return _encode_debug_message(seq, "pong", body)


def encode_action_command(
    session_id: str,
    command_id: str,
    requests: list[str],
    *,
    target: str = "",
    timeout_ms: int = 9500,
) -> bytes:
    payload = bytearray()
    payload += _put_bytes(1, session_id)
    payload += _put_bytes(2, command_id)
    for request in requests:
        payload += _put_bytes(3, request)
    if target:
        payload += _put_bytes(4, target)
    if timeout_ms != 9500:
        payload += _put_uint(5, timeout_ms)
    return bytes(payload)


def encode_action_command_base64(
    session_id: str,
    command_id: str,
    requests: list[str],
    *,
    target: str = "",
    timeout_ms: int = 9500,
) -> str:
    return base64.b64encode(
        encode_action_command(
            session_id,
            command_id,
            requests,
            target=target,
            timeout_ms=timeout_ms,
        )
    ).decode("ascii")


class _WebSocketConnection:
    def __init__(
        self,
        stream: socket.socket,
        on_message: Callable[[bytes], None],
        on_closed: Callable[["_WebSocketConnection"], None],
    ) -> None:
        self.stream = stream
        self.on_message = on_message
        self.on_closed = on_closed
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._fragments = bytearray()
        self._fragment_opcode = 0

    @staticmethod
    def _read_exact(stream: socket.socket, size: int) -> bytes:
        output = bytearray()
        while len(output) < size:
            chunk = stream.recv(size - len(output))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            output.extend(chunk)
        return bytes(output)

    def send(self, payload: bytes, opcode: int = 2) -> None:
        if self._closed.is_set():
            raise ConnectionError("WebSocket connection closed")
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(size)
        elif size <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack("!H", size))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", size))
        with self._send_lock:
            self.stream.sendall(bytes(header) + payload)

    def run(self) -> None:
        try:
            while not self._closed.is_set():
                first, second = self._read_exact(self.stream, 2)
                final = bool(first & 0x80)
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack("!H", self._read_exact(self.stream, 2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", self._read_exact(self.stream, 8))[0]
                mask = self._read_exact(self.stream, 4) if masked else b""
                payload = bytearray(self._read_exact(self.stream, size))
                if mask:
                    for index in range(len(payload)):
                        payload[index] ^= mask[index & 3]
                if opcode == 8:
                    break
                if opcode == 9:
                    self.send(bytes(payload), opcode=10)
                    continue
                if opcode in {1, 2} and not final:
                    self._fragment_opcode = opcode
                    self._fragments = payload
                    continue
                if opcode == 0:
                    self._fragments.extend(payload)
                    if not final:
                        continue
                    opcode = self._fragment_opcode
                    payload = self._fragments
                    self._fragments = bytearray()
                    self._fragment_opcode = 0
                if opcode in {1, 2}:
                    self.on_message(bytes(payload))
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.close()
            self.on_closed(self)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.stream.close()
        except OSError:
            pass


class WmpfCdpBridge:
    """Small local WebSocket/CDP bridge used by the desktop WeChat mini app."""

    def __init__(
        self,
        log: Callable[[str], None] | None = None,
        port: int = DEBUG_PORT,
    ) -> None:
        self.log = log or (lambda _message: None)
        self.port = port
        self._stop = threading.Event()
        self._server_ready = threading.Event()
        self._runtime_ready = threading.Event()
        self._setup_complete = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._clients: list[_WebSocketConnection] = []
        self._contexts: list[str] = []
        self._execution_contexts: dict[str, int] = {}
        self._enabled_contexts: set[str] = set()
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._seq = 0
        self._cdp_id = 0
        self._connection_generation = 0
        self._setup_generation = 0
        self._setup_started = False
        self._cancel_generation = 0
        # A JS page can be replaced without replacing the TCP connection.
        self._runtime_epoch = 0
        self._setup_epoch = 0
        self._relaunch_epoch = 0
        self._last_setup_error = ""
        # WMPF 25560 exposes the miniapp page as an already-attached CDP
        # target, but does not emit addJsContext.  Keep the CDP target session
        # id and route Runtime commands through it explicitly.
        self._target_session_id = ""
        self._appindex_target_id = ""

    @property
    def runtime_ready(self) -> bool:
        with self._lock:
            return bool(
                self._clients
                and self._runtime_ready.is_set()
                and self._setup_complete.is_set()
                and self._setup_generation == self._connection_generation
                and self._setup_epoch == self._runtime_epoch
            )

    @property
    def connection_generation(self) -> int:
        with self._lock:
            return self._connection_generation

    def _reset_runtime_state(self) -> None:
        self._runtime_epoch += 1
        self._runtime_ready.clear()
        self._setup_complete.clear()
        self._setup_generation = 0
        self._setup_started = False
        self._contexts.clear()
        self._execution_contexts.clear()
        self._enabled_contexts.clear()
        self._target_session_id = ""
        self._appindex_target_id = ""

    def diagnostic_state(self) -> str:
        with self._lock:
            return (f"连接={self._connection_generation}, 页面代次={self._runtime_epoch}, "
                    f"在线={len(self._clients)}, 页面数={len(self._contexts)}, "
                    f"初始化={self._setup_complete.is_set()}, 就绪={self.runtime_ready}, "
                    f"最后初始化错误={self._last_setup_error or '无'}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._server_ready.clear()
        self._thread = threading.Thread(
            target=self._server_main, name="desktop-wmpf-cdp", daemon=True
        )
        self._thread.start()
        if not self._server_ready.wait(3.0):
            raise RuntimeError("电脑版微信调试服务启动超时")
        if self._server is None:
            raise RuntimeError(
                f"本机端口 {self.port} 已被占用；请关闭其他电脑版微信采集软件后重试"
            )

    def _server_main(self) -> None:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", self.port))
            server.listen(4)
            server.settimeout(0.5)
            self._server = server
        except OSError:
            self._server = None
            self._server_ready.set()
            return
        self._server_ready.set()
        while not self._stop.is_set():
            try:
                stream, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            stream.settimeout(15.0)
            try:
                request = bytearray()
                while b"\r\n\r\n" not in request and len(request) < 65536:
                    chunk = stream.recv(4096)
                    if not chunk:
                        raise ConnectionError
                    request.extend(chunk)
                header_text = bytes(request).decode("latin-1", errors="ignore")
                headers: dict[str, str] = {}
                for line in header_text.split("\r\n")[1:]:
                    name, separator, value = line.partition(":")
                    if separator:
                        headers[name.strip().lower()] = value.strip()
                key = headers.get("sec-websocket-key", "")
                if not key:
                    raise ConnectionError
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
                            "ascii"
                        )
                    ).digest()
                ).decode("ascii")
                stream.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                stream.settimeout(None)
                client = _WebSocketConnection(
                    stream,
                    lambda _payload: None,
                    self._on_closed,
                )
                client.on_message = lambda payload, source=client: self._on_message(payload, source)
                with self._lock:
                    previous_clients = list(self._clients)
                    self._cancel_pending_locked(RuntimeError("电脑版微信调试连接已更换"))
                    self._reset_runtime_state()
                    self._clients = [client]
                    self._connection_generation += 1
                self.log(f"小程序调试连接已建立：{self.diagnostic_state()}")
                for previous in previous_clients:
                    previous.close()
                threading.Thread(target=client.run, daemon=True).start()
            except (OSError, ConnectionError):
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            server.close()
        except OSError:
            pass

    def _on_closed(self, client: _WebSocketConnection) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                if not self._clients:
                    self._cancel_pending_locked(RuntimeError("电脑版微信调试连接已断开"))
                    self._reset_runtime_state()

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _send_raw(self, payload: bytes) -> None:
        if self._stop.is_set():
            raise RuntimeError("电脑版微信调试通道已停止")
        with self._lock:
            clients = list(self._clients)
        if not clients:
            raise RuntimeError("电脑版微信小程序调试通道尚未连接")
        try:
            clients[-1].send(payload)
        except OSError as exc:
            raise RuntimeError(f"电脑版微信调试连接已断开：{exc}") from exc

    def _on_message(self, raw: bytes, source: _WebSocketConnection | None = None) -> None:
        # Serialise origin validation and state updates against page replacement.
        with self._lock:
            if source is not None and (not self._clients or source is not self._clients[-1]):
                return
            self._handle_message(raw)

    def _handle_message(self, raw: bytes) -> None:
        try:
            message = decode_debug_message(raw)
        except (ValueError, TypeError) as exc:
            self.log(f"电脑版微信调试消息解析失败：{exc}")
            return
        category = message.get("category")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        if category == "ping":
            self._send_raw(
                wrap_pong(
                    self._next_seq(),
                    int(data.get("ping_id") or 0),
                    str(data.get("payload") or ""),
                )
            )
            return
        if category == "setupContext":
            with self._lock:
                should_start = not self._setup_started
                if should_start:
                    self._setup_started = True
                    generation = self._connection_generation
                    epoch = self._runtime_epoch
            if should_start:
                threading.Thread(
                    target=self._initialize_debug_runtime,
                    args=(generation, epoch),
                    daemon=True,
                ).start()
            return
        # WMPF 25558 announces a page with addJsContext.  WMPF 25560 uses
        # connectJsContext for the same inbound registration in some launch
        # paths.  Treat both as context registration; otherwise the socket is
        # online but the bridge permanently reports 页面数=0.
        if category in {"addJsContext", "connectJsContext"}:
            context = str(data.get("jscontext_id") or "")
            if context:
                with self._lock:
                    if context not in self._contexts:
                        self._contexts.append(context)
                self._send_raw(wrap_connect_js_context(self._next_seq(), context))
                threading.Thread(
                    target=self._enable_context,
                    args=(context, self._connection_generation, self._runtime_epoch,
                          self._cancel_generation), daemon=True
                ).start()
            return
        if category == "removeJsContext":
            context = str(data.get("jscontext_id") or "")
            with self._lock:
                if context in self._contexts:
                    self._contexts.remove(context)
                self._execution_contexts.pop(context, None)
                self._enabled_contexts.discard(context)
            return
        if category not in {"chromeDevtools", "chromeDevtoolsResult"}:
            return
        try:
            payload = json.loads(str(data.get("payload") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        method = str(payload.get("method") or "") if isinstance(payload, dict) else ""
        params = payload.get("params") if isinstance(payload, dict) else {}
        if method == "Runtime.executionContextCreated" and isinstance(params, dict):
            context_data = params.get("context")
            if isinstance(context_data, dict):
                context_id = context_data.get("id")
                jscontext_id = str(data.get("jscontext_id") or "")
                if isinstance(context_id, int):
                    with self._lock:
                        self._execution_contexts[jscontext_id] = context_id
        elif method == "Runtime.executionContextDestroyed" and isinstance(params, dict):
            destroyed = params.get("executionContextId")
            with self._lock:
                for key, value in list(self._execution_contexts.items()):
                    if value == destroyed:
                        self._execution_contexts.pop(key, None)
        elif method == "Runtime.executionContextsCleared":
            with self._lock:
                self._execution_contexts.clear()

        # CDP's JSON id identifies the request. The surrounding WMPF operation
        # id is a transport sequence and can be zero or refer to another frame.
        # Notifications must never consume a pending request with that number.
        if method:
            return
        response_id = payload.get("id") if isinstance(payload, dict) else None
        op_id = (response_id if type(response_id) is int
                 else int(data.get("op_id") or 0))
        if not op_id:
            return
        with self._lock:
            pending = self._pending.pop(op_id, None)
        if pending is None or pending.done():
            return
        if isinstance(payload, dict) and payload.get("error"):
            pending.set_exception(RuntimeError(str(payload.get("error"))))
        else:
            pending.set_result(payload if isinstance(payload, dict) else {})

    def _initialize_debug_runtime(self, generation: int, epoch: int | None = None) -> None:
        with self._lock:
            epoch = self._runtime_epoch if epoch is None else epoch
            cancellation = self._cancel_generation
        resumed = False
        try:
            for method, params in (
                ("Runtime.enable", {}),
                ("Runtime.runIfWaitingForDebugger", {}),
                ("Target.setDiscoverTargets", {"discover": True}),
            ):
                with self._lock:
                    if epoch != self._runtime_epoch or generation != self._connection_generation:
                        return
                try:
                    self.call(method, params, timeout=2.0, _cancel_generation=cancellation)
                    if method == "Runtime.runIfWaitingForDebugger":
                        resumed = True
                except (RuntimeError, TimeoutError, FutureTimeoutError) as exc:
                    with self._lock:
                        if epoch == self._runtime_epoch:
                            self._last_setup_error = f"{method}: {str(exc) or type(exc).__name__}"
            # WMPF 25560 exposes the mini-program as an AppIndex target but
            # does not reliably announce a legacy jscontext.  Attach the live
            # target during initialization so Runtime.evaluate has a real
            # flattened page session before the first route probe.
            self._bind_appindex_target(generation, epoch, cancellation, attach=True)
        finally:
            with self._lock:
                if (not self._stop.is_set()
                        and generation == self._connection_generation and self._clients
                        and epoch == self._runtime_epoch and resumed):
                    self._setup_generation = generation
                    self._setup_epoch = epoch
                    self._setup_complete.set()
                    self._runtime_ready.set()
                    self._last_setup_error = ""
                elif generation == self._connection_generation and epoch == self._runtime_epoch:
                    self._setup_started = False
            self.log(f"小程序初始化检查：{self.diagnostic_state()}")

    def _bind_appindex_target(self, generation: int, epoch: int,
                              cancellation: int, *, attach: bool = True) -> None:
        """Bind the current wxca8d4b8e8feedc2a AppIndex CDP target.

        WMPF 25560 reports setupContext but suppresses the page-context
        notification.  The target is still visible through Target.getTargets;
        attaching it yields the sessionId required for Runtime.evaluate.
        """
        try:
            targets = self.call("Target.getTargets", {}, timeout=2.0,
                                _cancel_generation=cancellation)
            infos = ((targets.get("result") or {}).get("targetInfos")
                     if isinstance(targets, dict) else []) or []
            app_targets = [item for item in infos
                           if isinstance(item, dict)
                           and item.get("type") in {"page", "webview"}
                           and "wxca8d4b8e8feedc2a" in str(item.get("url") or "")]
            if app_targets:
                summary = "; ".join(
                    f"{item.get('title','')}|{item.get('type','')}|{item.get('targetId','')}|{item.get('url','')}"
                    for item in app_targets
                )
                self.log(f"小店调试目标：{summary[:1200]}")
            # AppIndex is the preferred page, but 25560 occasionally reports
            # the same app page under a generated title during a route swap.
            # Binding only the literal AppIndex target leaves a live window
            # with a dead CDP session, so accept any app-owned page as a
            # fallback and let route/state verification choose the usable one.
            candidate = next((item for item in app_targets
                              if item.get("title") == "AppIndex"), None)
            candidate = candidate or (app_targets[0] if app_targets else None)
            if not candidate:
                return
            with self._lock:
                if (generation == self._connection_generation
                        and epoch == self._runtime_epoch):
                    self._appindex_target_id = str(candidate.get("targetId") or "")
            if not attach:
                return
            attached = self.call(
                "Target.attachToTarget",
                {"targetId": str(candidate.get("targetId") or ""), "flatten": True},
                timeout=3.0, _cancel_generation=cancellation,
            )
            session_id = str(((attached.get("result") or {}).get("sessionId"))
                             if isinstance(attached, dict) else "")
            if session_id:
                with self._lock:
                    if (generation == self._connection_generation
                            and epoch == self._runtime_epoch):
                        self._target_session_id = session_id
                # Enable the Runtime domain on the newly attached page
                # session. The browser-level Runtime.enable above does not
                # apply to a flattened target session.
                self.call("Runtime.enable", {}, timeout=3.0,
                          _cancel_generation=cancellation)
                self.call("Runtime.runIfWaitingForDebugger", {}, timeout=3.0,
                          _cancel_generation=cancellation)
                self.log("已绑定小店 AppIndex 页面调试会话")
        except (RuntimeError, TimeoutError, FutureTimeoutError) as exc:
            self.log(f"小店 AppIndex 页面绑定未完成，将继续重试：{exc}")

    def refresh_target_binding(self) -> None:
        """Reattach the current app page after a WMPF route handover.

        WMPF 25560 can keep the transport socket alive while replacing the
        page target during wxShopHomeLite -> globalSearch navigation.  The
        old flattened session then accepts no Runtime commands and every
        evaluate call times out.  Re-querying Target.getTargets and attaching
        the current app-owned page repairs that handover without reopening
        the mini-program.
        """
        with self._lock:
            generation = self._connection_generation
            epoch = self._runtime_epoch
            cancellation = self._cancel_generation
        if not generation or not self._clients:
            return
        self._bind_appindex_target(generation, epoch, cancellation)

    def _enable_context(self, context: str, generation: int | None = None,
                        epoch: int | None = None, cancellation: int | None = None) -> None:
        with self._lock:
            generation = self._connection_generation if generation is None else generation
            epoch = self._runtime_epoch if epoch is None else epoch
            cancellation = self._cancel_generation if cancellation is None else cancellation
        enabled = False
        for method, params in (
            ("Runtime.enable", {}),
            ("Runtime.runIfWaitingForDebugger", {}),
        ):
            with self._lock:
                if epoch != self._runtime_epoch or generation != self._connection_generation:
                    return
            try:
                self.call(method, params, timeout=3.0, jscontext_id=context,
                          _cancel_generation=cancellation)
                enabled = True
            except (RuntimeError, TimeoutError, FutureTimeoutError):
                pass
        if enabled:
            with self._lock:
                if (context in self._contexts and epoch == self._runtime_epoch
                        and generation == self._connection_generation):
                    self._enabled_contexts.add(context)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
        jscontext_id: str = "",
        _cancel_generation: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            generation = self._cancel_generation if _cancel_generation is None else _cancel_generation
            self._check_generation(generation)
            self._cdp_id += 1
            op_id = self._cdp_id
            future: Future[dict[str, Any]] = Future()
            self._pending[op_id] = future
        request_body = {"id": op_id, "method": method, "params": params or {}}
        with self._lock:
            target_session = self._target_session_id
        if target_session and not method.startswith("Target."):
            request_body["sessionId"] = target_session
        request = json.dumps(
            request_body,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            if jscontext_id:
                frame = wrap_cdp_context_payload(
                    request, self._next_seq(), op_id, jscontext_id
                )
            else:
                frame = wrap_cdp_payload(request, self._next_seq(), op_id)
            with self._lock:
                self._check_generation(generation)
                self._send_raw(frame)
            try:
                result = future.result(timeout=max(0.1, timeout))
            except FutureTimeoutError as exc:
                raise CdpTimeoutError(
                    f"页面调试命令超时：{method}，页面 {jscontext_id or 'default'}，等待 {timeout:g} 秒"
                ) from exc
            self._check_generation(generation)
            return result
        finally:
            with self._lock:
                self._pending.pop(op_id, None)

    def wait_runtime(self, timeout: float = 10.0, *, cancel_event: threading.Event | None = None) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        next_probe = 0.0
        while time.monotonic() < deadline:
            if self._stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return False
            if self.runtime_ready:
                return True
            now = time.monotonic()
            with self._lock:
                has_client = bool(self._clients)
            # A navigation/relaunch can briefly have no websocket client.
            # Retain the bounded reconnect window; cancellation still wins.
            if has_client and now >= next_probe:
                next_probe = now + 0.8
                try:
                    self.call("Runtime.enable", {}, timeout=min(0.6, max(0.1, deadline - now)))
                except (RuntimeError, TimeoutError, FutureTimeoutError):
                    pass
            self._setup_complete.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        return self.runtime_ready

    def wait_runtime_refresh(self, generation: int, timeout: float = 20.0, *, cancel_event: threading.Event | None = None,
                             abort_event: threading.Event | None = None) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self._stop.is_set() or (cancel_event is not None and cancel_event.is_set())
                    or (abort_event is not None and abort_event.is_set())):
                return False
            with self._lock:
                fresh_page = (self._relaunch_epoch > 0
                              and self._setup_epoch >= self._relaunch_epoch)
                if (self.connection_generation > generation or fresh_page) and self.runtime_ready:
                    return True
            self._stop.wait(0.1)
        return False

    def evaluate_all(self, expression: str, timeout: float = 10.0) -> list[Any]:
        with self._lock:
            generation = self._cancel_generation
        if not self.wait_runtime(min(timeout, 3.0)):
            raise RuntimeError("电脑版微信小程序运行环境尚未就绪")
        with self._lock:
            # WMPF 25560 may omit the addJsContext notification even though
            # the embedded page is the already-attached AppIndex target.  The
            # public debugger uses that target name when no explicit page
            # context is announced, so probe it before falling back to the
            # browser/default channel.
            contexts = list(dict.fromkeys(self._contexts)) or (
                ([self._appindex_target_id, "AppIndex"]
                 if self._appindex_target_id else ["AppIndex", ""])
                if not self._target_session_id else [""]
            )
            execution_contexts = dict(self._execution_contexts)
        values: list[Any] = []
        last_error: Exception | None = None
        for context in contexts:
            self._check_generation(generation)
            params: dict[str, Any] = {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            }
            execution_id = execution_contexts.get(context)
            if execution_id is not None:
                params["contextId"] = execution_id
            retried_binding = False
            try:
                reply = self.call(
                    "Runtime.evaluate",
                    params,
                    timeout=timeout,
                    jscontext_id=context,
                    _cancel_generation=generation,
                )
            except CdpCancelledError:
                raise
            except (TimeoutError, FutureTimeoutError) as exc:
                self._check_generation(generation)
                # The page target can be replaced without a new websocket.
                # Refresh the flattened target once before declaring the
                # context dead; this is the common 4.1.13.65 handover path.
                if not retried_binding and self._target_session_id:
                    retried_binding = True
                    try:
                        self.refresh_target_binding()
                        with self._lock:
                            execution_contexts = dict(self._execution_contexts)
                        retry_params = dict(params)
                        retry_execution_id = execution_contexts.get(context)
                        if retry_execution_id is not None:
                            retry_params["contextId"] = retry_execution_id
                        reply = self.call(
                            "Runtime.evaluate", retry_params, timeout=timeout,
                            jscontext_id=context, _cancel_generation=generation,
                        )
                    except (RuntimeError, TimeoutError, FutureTimeoutError) as retry_exc:
                        # Some 25560 builds expose Target.attachToTarget but
                        # reject flattened Runtime.evaluate commands.  Fall
                        # back to the legacy AppIndex/default routing once;
                        # the page itself is still alive and this path is
                        # what older WMPF builds use successfully.
                        with self._lock:
                            self._target_session_id = ""
                        fallback_error: Exception = retry_exc
                        for fallback_context in ("AppIndex", ""):
                            try:
                                reply = self.call(
                                    "Runtime.evaluate", params, timeout=timeout,
                                    jscontext_id=fallback_context,
                                    _cancel_generation=generation,
                                )
                                fallback_error = None  # type: ignore[assignment]
                                break
                            except (RuntimeError, TimeoutError, FutureTimeoutError) as fallback_exc:
                                fallback_error = fallback_exc
                        if fallback_error is not None:
                            last_error = CdpTimeoutError(
                                str(fallback_error) or str(exc) or
                                f"页面调试命令超时：Runtime.evaluate，页面 {context or 'default'}，等待 {timeout:g} 秒"
                            )
                            continue
                else:
                    last_error = CdpTimeoutError(
                        str(exc) or f"页面调试命令超时：Runtime.evaluate，页面 {context or 'default'}，等待 {timeout:g} 秒"
                    )
                    continue
            except Exception as exc:
                self._check_generation(generation)
                last_error = exc
                continue
            self._check_generation(generation)
            # call() returns the full CDP response envelope. Runtime.evaluate
            # puts its RemoteObject and exceptionDetails inside response.result.
            evaluation = reply.get("result") or {}
            if evaluation.get("exceptionDetails"):
                last_error = RuntimeError(str(evaluation.get("exceptionDetails")))
                continue
            result = evaluation.get("result")
            if isinstance(result, dict):
                if "value" in result:
                    values.append(result["value"])
                elif result.get("description"):
                    values.append(result["description"])
        if not values and last_error is not None:
            raise last_error
        return values

    def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        values = self.evaluate_all(expression, timeout)
        for value in values:
            if value not in (None, "", "undefined"):
                return value
        return values[0] if values else None

    def _check_generation(self, generation: int) -> None:
        with self._lock:
            if self._stop.is_set() or generation != self._cancel_generation:
                raise CdpCancelledError("电脑版微信旧页面任务已停止，不能继续使用旧连接")

    def _cancel_pending_locked(self, error: Exception) -> None:
        self._cancel_generation += 1
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def cancel_pending(self) -> None:
        """Wake callers without closing the page's persistent debug socket."""
        with self._lock:
            self._cancel_pending_locked(CdpCancelledError("电脑版微信调试通道已停止"))

    def prepare_relaunch(self) -> None:
        """Invalidate the old page, but let WeChat complete its socket handover.

        Closing that socket here breaks the next LaunchApplet handshake on
        WMPF 25558. Only retire it after the replacement client is accepted.
        A fresh setup on the retained socket is valid too; stale ready flags
        and workers from an older page epoch must never validate a relaunch.
        """
        with self._lock:
            self._cancel_pending_locked(RuntimeError("电脑版微信小程序已关闭，正在重建页面连接"))
            self._reset_runtime_state()
            self._relaunch_epoch = self._runtime_epoch

    def stop(self) -> None:
        self._stop.set()
        self.cancel_pending()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.close()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._clients.clear()
            self._reset_runtime_state()
        self._server_ready.clear()
