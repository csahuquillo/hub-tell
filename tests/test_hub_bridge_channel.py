#!/usr/bin/env python3
"""Tests for the hub-bridge MCP channel: tools, notifications and the stdio protocol."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bridge/hub_bridge_channel.py"
SPEC = importlib.util.spec_from_file_location("hub_bridge_channel", SCRIPT)
channel = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(channel)


def fake_commands(calls, request_id):
    def fake(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, f"{request_id}\n" if args[1] == "send" else "", "")
    return fake


class ChannelToolTests(unittest.TestCase):
    def test_ask_dispatches_once_and_marks_the_envelope_delivered(self):
        calls = []
        with mock.patch.object(channel, "SLOT", "api"), \
             mock.patch.object(channel, "live_slots", return_value=[{"name": "slot-worker", "status": "idle"}]), \
             mock.patch.object(channel, "command", side_effect=fake_commands(calls, "request-1")):
            result = channel.tool_call("ask_codex", {"codex": "codex:worker", "message": "look at this"})
        self.assertIn("answer will arrive", result)
        self.assertEqual(calls[0][1:7], ("send", "codex:slot-worker", "look at this", "--from", "claude:api", "--type"))
        dispatches = [c for c in calls if c[1] == "dispatch"]
        self.assertEqual(dispatches, [(str(channel.HUB_CODEX), "dispatch", "slot-worker", "look at this", "--from",
                                       "claude:api", "--request-id", "request-1", "--hops", "0")])
        self.assertIn((str(channel.HUB_BUS), "deliver", "request-1", "--by", "claude:api"), calls)

    def test_ask_unknown_slot_fails_without_side_effects(self):
        calls = []
        with mock.patch.object(channel, "live_slots", return_value=[]), \
             mock.patch.object(channel, "command", side_effect=fake_commands(calls, "x")):
            with self.assertRaises(ValueError):
                channel.tool_call("ask_codex", {"codex": "ghost", "message": "hi"})
        self.assertEqual(calls, [])

    def test_list_codex_uses_live_inventory(self):
        with mock.patch.object(channel, "live_slots", return_value=[{"name": "slot-a", "status": "working"}]):
            self.assertEqual(channel.tool_call("list_codex", {}), "codex:slot-a (working)")

    def test_open_codex_creates_visible_slot_then_dispatches_first_task(self):
        calls = []
        with mock.patch.object(channel, "SLOT", "docs"), \
             mock.patch.object(channel, "command", side_effect=fake_commands(calls, "request-2")):
            result = channel.tool_call("open_codex", {"name": "reviewer", "task": "Review the change"})
        self.assertIn("codex:slot-reviewer", result)
        self.assertEqual(calls[0], (str(channel.HUB_CODEX), "open", "slot-reviewer", "--created-by", "claude:docs"))
        self.assertIn((str(channel.HUB_CODEX), "dispatch", "slot-reviewer", "Review the change", "--from",
                       "claude:docs", "--request-id", "request-2", "--hops", "0"), calls)

    def test_recent_incoming_hops_are_carried_into_outgoing_messages(self):
        calls = []
        with mock.patch.object(channel, "SLOT", "api"), \
             mock.patch.object(channel, "CURRENT_HOPS", 3), \
             mock.patch.object(channel, "HOPS_AT", time.monotonic()), \
             mock.patch.object(channel, "live_slots", return_value=[{"name": "slot-w", "status": "idle"}]), \
             mock.patch.object(channel, "command", side_effect=fake_commands(calls, "r")):
            channel.tool_call("ask_codex", {"codex": "w", "message": "again"})
        self.assertEqual(calls[0][-1], "3")

    def test_notification_meta_values_are_strings(self):
        note = channel.channel_notification({"from": "codex:slot-a", "text": "hi", "hops": 2})
        self.assertEqual(note["method"], "notifications/claude/channel")
        self.assertEqual(note["params"]["meta"], {"sender": "codex:slot-a", "hops": "2"})


class ChannelProcessTests(unittest.TestCase):
    def test_stdio_protocol_and_socket_push(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "HUB_SLOT": "docs", "HUB_BRIDGE_STATE": directory}
            proc = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)
            try:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                             "params": {"protocolVersion": "2025-06-18"}}) + "\n")
                proc.stdin.flush()
                init = json.loads(proc.stdout.readline())
                self.assertIn("claude/channel", init["result"]["capabilities"]["experimental"])
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
                proc.stdin.flush()
                tools = {t["name"] for t in json.loads(proc.stdout.readline())["result"]["tools"]}
                self.assertEqual(tools, {"ask_codex", "list_codex", "open_codex"})
                sock_path = Path(directory) / "docs.sock"
                self.assertEqual(sock_path.stat().st_mode & 0o777, 0o600)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(sock_path))
                    client.sendall((json.dumps({"from": "codex:slot-a", "text": "result", "hops": 1}) + "\n").encode())
                note = json.loads(proc.stdout.readline())
                self.assertEqual(note["params"]["content"], "result")
                self.assertEqual(note["params"]["meta"]["hops"], "1")
            finally:
                proc.kill()
                proc.wait()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()


if __name__ == "__main__":
    unittest.main()
