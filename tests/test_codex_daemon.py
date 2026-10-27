#!/usr/bin/env python3
"""Protocol tests for bridge/codex_daemon.py against a fake App Server on a temporary Unix socket."""

import base64
import hashlib
import importlib.util
import json
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_daemon", ROOT / "bridge/codex_daemon.py")
daemon = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(daemon)


def read_exact(sock, amount):
    data = bytearray()
    while len(data) < amount:
        chunk = sock.recv(amount - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data.extend(chunk)
    return bytes(data)


def recv_frame(sock):
    first, second = read_exact(sock, 2)
    length = second & 127
    if length == 126:
        length = struct.unpack(">H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(sock, 8))[0]
    mask = read_exact(sock, 4) if second & 128 else b""
    payload = read_exact(sock, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return first & 15, payload


def send_frame(sock, value, *, fin=True, opcode=1):
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    header = bytearray([((0x80 if fin else 0) | opcode)])
    if len(payload) < 126:
        header.append(len(payload))
    elif len(payload) < 65536:
        header.append(126)
        header.extend(struct.pack(">H", len(payload)))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", len(payload)))
    sock.sendall(bytes(header) + payload)


class FakeDaemon:
    """Speaks just enough WebSocket + JSON-RPC to exercise the client."""

    def __init__(self, path, busy=False):
        self.path, self.busy = path, busy
        self.calls, self.error_next = [], False
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.server.accept()
        with conn:
            headers = bytearray()
            while b"\r\n\r\n" not in headers:
                headers.extend(conn.recv(1024))
            text = headers.decode("ascii")
            key = next(line.split(":", 1)[1].strip() for line in text.split("\r\n")
                       if line.lower().startswith("sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1((key + daemon.WS_GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            while True:
                try:
                    opcode, raw = recv_frame(conn)
                except (OSError, ConnectionError):
                    return
                if opcode == 8:
                    return
                request = json.loads(raw)
                if "id" not in request:
                    continue
                self.calls.append(request)
                method, rid = request["method"], request["id"]
                if self.error_next:
                    self.error_next = False
                    send_frame(conn, {"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "fake failure"}})
                    continue
                if method == "thread/start":
                    result = {"thread": {"id": "thread-fake"}}
                elif method == "thread/resume":
                    result = {"thread": {"id": "thread-fake", "status": {"type": "active" if self.busy else "idle"}}}
                elif method == "thread/read":
                    result = {"thread": {"id": "thread-fake", "name": "slot-test"}}
                elif method == "thread/list":
                    result = {"threads": [{"id": "thread-fake"}]}
                else:
                    result = {}
                send_frame(conn, {"jsonrpc": "2.0", "id": rid, "result": result})
                if method == "turn/start":
                    # A server ping must be answered with a pong carrying the same payload.
                    conn.sendall(bytes([0x89, 4]) + b"ping")
                    opcode, pong = recv_frame(conn)
                    if opcode != 10 or pong != b"ping":
                        return
                    send_frame(conn, {"method": "item/completed", "params": {"item": {
                        "type": "agentMessage", "text": "let me check first"}}})
                    event = json.dumps({"method": "turn/completed", "params": {"turn": {
                        "status": "completed", "items": [{"type": "agentMessage", "text": "fake answer"}]}}}).encode()
                    split = len(event) // 2
                    send_frame(conn, event[:split], fin=False, opcode=1)
                    send_frame(conn, event[split:], fin=True, opcode=0)

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass


class CodexDaemonTests(unittest.TestCase):
    def with_daemon(self, action, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "app-server.sock")
            server = FakeDaemon(path, **kwargs)
            try:
                action(path, server)
            finally:
                server.close()

    def test_create_turn_read_list_archive_and_delete(self):
        def run(path, server):
            client = daemon.connect_daemon(path)
            try:
                thread_id = client.create_thread("slot-test", cwd="/tmp", sandbox="read-only")
                self.assertEqual(thread_id, "thread-fake")
                self.assertEqual(client.run_turn(thread_id, "hello", timeout=2), "fake answer")
                self.assertEqual(client.read_thread(thread_id)["thread"]["id"], thread_id)
                self.assertEqual(client.list_threads()[0]["id"], thread_id)
                client.archive_thread(thread_id)
                client.delete_thread(thread_id)
                methods = [call["method"] for call in server.calls]
                for expected in ("initialize", "thread/name/set", "thread/resume", "turn/start",
                                 "thread/archive", "thread/delete"):
                    self.assertIn(expected, methods)
                start = next(c for c in server.calls if c["method"] == "thread/start")
                self.assertEqual(start["params"]["approvalPolicy"], "never")
                resume = next(c for c in server.calls if c["method"] == "thread/resume")
                self.assertTrue(resume["params"]["excludeTurns"])
            finally:
                client.close()
        self.with_daemon(run)

    def test_busy_thread_raises_thread_busy_without_starting_a_turn(self):
        def run(path, server):
            client = daemon.connect_daemon(path)
            try:
                with self.assertRaises(daemon.ThreadBusy):
                    client.run_turn("thread-fake", "hello", timeout=2)
                self.assertNotIn("turn/start", [call["method"] for call in server.calls])
            finally:
                client.close()
        self.with_daemon(run, busy=True)

    def test_jsonrpc_errors_are_exceptions(self):
        def run(path, server):
            client = daemon.connect_daemon(path)
            server.error_next = True
            try:
                with self.assertRaises(daemon.JSONRPCError):
                    client.read_thread("x")
            finally:
                client.close()
        self.with_daemon(run)

    def test_missing_socket_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(daemon.CodexDaemonError):
                daemon.CodexDaemon(str(Path(directory) / "nothing.sock"), timeout=1, retries=1)


if __name__ == "__main__":
    unittest.main()
