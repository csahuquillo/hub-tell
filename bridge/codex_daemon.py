#!/usr/bin/env python3
"""Minimal JSON-RPC over WebSocket client for the ``codex app-server`` daemon.

The daemon listens on a Unix socket (by default
``$CODEX_HOME/app-server-control/app-server-control.sock``). Threads created
or resumed through it are the same threads Codex Desktop shows, so a human can
watch a slot work and answer it from the app.

Only the standard library is used: the WebSocket framing is implemented here
(client frames are masked, server pings are answered, fragmented text frames
are reassembled).

The App Server protocol is experimental; method names follow the version this
was written against and may change upstream.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
SOCK = os.environ.get("HUB_CODEX_DAEMON_SOCKET",
                      str(CODEX_HOME / "app-server-control" / "app-server-control.sock"))
DEFAULT_CWD = os.environ.get("HUB_CODEX_DAEMON_CWD", str(Path.home()))
MAX_MESSAGE = 16 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CodexDaemonError(RuntimeError):
    pass


class ThreadBusy(CodexDaemonError):
    """The thread already has a turn running; the message must wait or be steered."""


class TurnTimeout(CodexDaemonError):
    def __init__(self, thread_id: str, turn_id: str | None, partial_text: str = ""):
        super().__init__(f"timeout waiting for the turn on {thread_id}")
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.partial_text = partial_text


class JSONRPCError(CodexDaemonError):
    def __init__(self, method: str, error: dict[str, Any]):
        self.method, self.error = method, error
        super().__init__(f"JSON-RPC {method}: {error.get('message', error)}")


class CodexDaemon:
    def __init__(self, sock: str | None = None, timeout: float = 30, retries: int = 3):
        self.path, self.timeout, self.retries = sock or SOCK, timeout, retries
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.next_id = 0
        self.pending: deque[dict[str, Any]] = deque()
        self.initialized = False
        self.connect()

    # -- transport ---------------------------------------------------------

    def connect(self) -> None:
        last: BaseException | None = None
        for attempt in range(self.retries):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.path)
                key = base64.b64encode(os.urandom(16)).decode("ascii")
                s.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise ConnectionError("the daemon closed the handshake")
                    response.extend(chunk)
                    if len(response) > 65536:
                        raise CodexDaemonError("handshake response too large")
                header, remainder = bytes(response).split(b"\r\n\r\n", 1)
                expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest())
                if b" 101 " not in header.split(b"\r\n", 1)[0] or expected not in header:
                    raise CodexDaemonError("invalid WebSocket handshake")
                self.sock, self.buffer = s, bytearray(remainder)
                return
            except (OSError, ConnectionError, CodexDaemonError) as exc:
                last = exc
                s.close()
                if attempt + 1 < self.retries:
                    time.sleep(min(0.1 * (2 ** attempt), 1.0))
        raise CodexDaemonError(f"could not connect to the Codex daemon: {last}")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None
                self.initialized = False

    def _send(self, obj: dict[str, Any]) -> None:
        if self.sock is None:
            self.connect()
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", len(data)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", len(data)))
        mask = os.urandom(4)
        frame = bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        assert self.sock is not None
        self.sock.sendall(frame)

    def _read_exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            if self.sock is None:
                raise ConnectionError("WebSocket not connected")
            chunk = self.sock.recv(max(4096, count - len(self.buffer)))
            if not chunk:
                self.close()
                raise ConnectionError("daemon WebSocket closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def _control(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            return
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> dict[str, Any]:
        fragments = bytearray()
        fragmented = False
        while True:
            first, second = self._read_exact(2)
            fin, opcode, masked = bool(first & 0x80), first & 0x0F, bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            if length > MAX_MESSAGE:
                raise CodexDaemonError("WebSocket message exceeds 16 MiB")
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 9:  # ping -> pong
                self._control(10, payload)
                continue
            if opcode == 10:  # pong
                continue
            if opcode == 8:  # close
                self.close()
                raise ConnectionError("the daemon sent close")
            if opcode == 1:
                fragments = bytearray(payload)
                fragmented = not fin
            elif opcode == 0 and fragmented:
                fragments.extend(payload)
            else:
                continue
            if fin:
                try:
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexDaemonError(f"invalid WebSocket JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise CodexDaemonError("JSON-RPC message is not an object")
                return value

    # -- JSON-RPC ------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout: float | None = None) -> dict[str, Any]:
        if method != "initialize" and not self.initialized:
            if self.sock is None:
                self.connect()
            self.initialize()
        elif self.sock is None:
            self.connect()
        self.next_id += 1
        rid = self.next_id
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        except (OSError, socket.timeout) as exc:
            self.close()
            raise CodexDaemonError(f"connection lost while sending {method}; not retried") from exc
        old_timeout = self.sock.gettimeout() if self.sock else self.timeout
        if self.sock:
            self.sock.settimeout(timeout or self.timeout)
        try:
            while True:
                value = self.recv()
                if value.get("id") == rid:
                    if "error" in value:
                        raise JSONRPCError(method, value["error"])
                    return value
                self.pending.append(value)
        except (socket.timeout, TimeoutError) as exc:
            self.close()
            raise CodexDaemonError(f"timeout in {method}") from exc
        except ConnectionError as exc:
            self.close()
            # Never resend: the request may already have been executed.
            raise CodexDaemonError(f"connection lost during {method}; not retried") from exc
        finally:
            if self.sock:
                self.sock.settimeout(old_timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def initialize(self) -> None:
        self.initialized = True  # set first so request() does not recurse
        try:
            self.request("initialize", {"clientInfo": {"name": "hub-codex", "version": "2.0"}})
            self.notify("initialized")
        except BaseException:
            self.initialized = False
            raise

    def request_event(self, timeout: float) -> dict[str, Any]:
        if self.pending:
            return self.pending.popleft()
        if self.sock:
            self.sock.settimeout(timeout)
        return self.recv()

    # -- threads and turns ---------------------------------------------------

    def create_thread(self, name: str, cwd: str = DEFAULT_CWD, sandbox: str = "workspace-write") -> str:
        # approvalPolicy=never: nobody answers approval prompts on the bus, so a
        # turn that asked for one would just hang until its timeout.
        result = self.request("thread/start", {
            "cwd": cwd, "sandbox": sandbox, "approvalPolicy": "never",
            "config": {"sandbox_workspace_write": {"writable_roots": [], "network_access": False}}})
        thread_id = result.get("result", {}).get("thread", {}).get("id")
        if not thread_id:
            raise CodexDaemonError("thread/start returned no thread id")
        self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return thread_id

    def is_busy(self, thread_id: str) -> bool:
        """True if the thread has a turn running (e.g. someone is using it in Codex Desktop)."""
        # excludeTurns: a long thread would otherwise return its whole history
        # (easily more than 16 MiB), which is not needed here.
        result = self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        status = result.get("result", {}).get("thread", {}).get("status") or {}
        return status.get("type") == "active"

    def active_turn_id(self, thread_id: str) -> str | None:
        result = self.request("thread/turns/list", {"threadId": thread_id, "limit": 3})
        for turn in result.get("result", {}).get("data", []) or []:
            if turn.get("status") == "inProgress":
                return turn.get("id")
        return None

    def inject_turn(self, thread_id: str, text: str) -> str | None:
        """Steer the active turn; return None if the thread became idle."""
        turn_id = self.active_turn_id(thread_id)
        if not turn_id:
            return None
        self.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                     "input": [{"type": "text", "text": text}]})
        return turn_id

    def list_turns(self, thread_id: str, *, items_view: str = "notLoaded",
                   limit: int = 10, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "itemsView": items_view, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self.request("thread/turns/list", params).get("result", {})

    def run_turn(self, thread_id: str, text: str, timeout: float = 900) -> str:
        if self.is_busy(thread_id):
            raise ThreadBusy(thread_id)
        started = self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})
        turn_id = (started.get("result", {}).get("turn") or {}).get("id")
        deadline = time.monotonic() + timeout
        final = ""
        while time.monotonic() < deadline:
            try:
                event = self.request_event(max(0.1, deadline - time.monotonic()))
            except (socket.timeout, TimeoutError):
                break
            method = event.get("method")
            if method == "turn/completed":
                turn = event.get("params", {}).get("turn", {})
                if turn.get("status") != "completed" or turn.get("error"):
                    raise CodexDaemonError(f"turn failed: {turn.get('error') or turn.get('status')}")
                items = [i.get("text", "") for i in turn.get("items", []) if i.get("type") == "agentMessage"]
                return "\n".join(items) or final
            # Only turn/completed ends a turn: Codex emits intermediate agent
            # messages ("let me check...") before running commands.
            if method == "item/completed":
                item = event.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage":
                    final = item.get("text", final)
        raise TurnTimeout(thread_id, turn_id, final)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request("thread/read", {"threadId": thread_id}).get("result", {})

    def archive_thread(self, thread_id: str) -> None:
        self.request("thread/archive", {"threadId": thread_id})

    def delete_thread(self, thread_id: str) -> None:
        self.request("thread/delete", {"threadId": thread_id})

    def list_threads(self) -> list[dict[str, Any]]:
        return self.request("thread/list", {}).get("result", {}).get("threads", [])


def connect_daemon(sock: str | None = None, timeout: float = 30) -> CodexDaemon:
    client = CodexDaemon(sock, timeout)
    client.initialize()
    return client
