#!/usr/bin/env python3
"""Tests for hub-bus-relay: delivery of undispatched envelopes and the claim broker."""

import datetime as dt
import importlib.util
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hub_bus_relay", ROOT / "hub-bus-relay.py")
relay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(relay)


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RelayControlTests(unittest.TestCase):
    def fake_call(self, seen, claim_output):
        def fake(*args, **kwargs):
            seen.append(args)
            if args[0] == str(relay.CODEX):
                return completed("codex:slot-reviewer\n")
            return completed(claim_output)
        return fake

    def test_claim_runs_with_the_resolved_slot_identity(self):
        seen = []
        with mock.patch.object(relay, "CLAIM", "/opt/claim"), \
             mock.patch.object(relay, "call", side_effect=self.fake_call(seen, "ACQUIRED repo:demo\n")):
            result = relay.handle_control({"operation": "acquire", "resource": "repo:demo",
                                           "purpose": "change module", "thread_id": "thread-full-id",
                                           "identity": "codex:slot-someone-else"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["identity"], "codex:slot-reviewer")
        self.assertEqual(seen[-1], ("/opt/claim", "acquire", "repo:demo", "--purpose", "change module",
                                    "--from", "codex:slot-reviewer"))

    def test_release_uses_the_same_bound_identity(self):
        seen = []
        with mock.patch.object(relay, "CLAIM", "/opt/claim"), \
             mock.patch.object(relay, "call", side_effect=self.fake_call(seen, "RELEASED\n")):
            result = relay.handle_control({"operation": "release", "resource": "repo:demo",
                                           "thread_id": "thread-full-id"})
        self.assertTrue(result["ok"])
        self.assertEqual(seen[-1][-2:], ("--from", "codex:slot-reviewer"))

    def test_without_backend_claims_fail_clearly_but_identity_resolves(self):
        seen = []
        with mock.patch.object(relay, "CLAIM", ""), \
             mock.patch.object(relay, "call", side_effect=self.fake_call(seen, "")):
            claim = relay.handle_control({"operation": "acquire", "resource": "r", "purpose": "p",
                                          "thread_id": "thread-full-id"})
            identity = relay.handle_control({"operation": "resolve-thread", "thread_id": "thread-full-id"})
        self.assertFalse(claim["ok"])
        self.assertIn("HUB_CLAIM_COMMAND", claim["result"])
        self.assertEqual(identity, {"ok": True, "identity": "codex:slot-reviewer"})

    def test_missing_thread_id_is_rejected(self):
        self.assertFalse(relay.handle_control({"operation": "resolve-thread"})["ok"])


class RelayDeliveryTests(unittest.TestCase):
    def message(self, directory, created_at, **extra):
        path = Path(directory) / "1793-abc.json"
        value = {"id": "1793-abc", "from": "claude:api", "to": "codex:slot-x", "body": "hello",
                 "created_at": created_at, "status": "queued", "delivered_at": None, **extra}
        path.write_text(json.dumps(value))
        return path

    def test_recent_message_waits_grace_so_sender_dispatch_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(relay, "deliver_codex") as deliver:
            path = self.message(directory, dt.datetime.now(dt.timezone.utc).isoformat())
            self.assertFalse(relay.dispatch_one(path))
            deliver.assert_not_called()

    def test_old_undispatched_message_is_delivered_and_marked(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(relay, "deliver_codex") as deliver, \
             mock.patch.object(relay, "call", return_value=completed()) as call:
            path = self.message(directory, "2020-01-01T00:00:00+00:00")
            self.assertTrue(relay.dispatch_one(path))
            deliver.assert_called_once()
            self.assertEqual(call.call_args.args[1:5], ("deliver", "1793-abc", "--by", "codex:slot-x"))

    def test_deferred_message_waits_for_next_delivery_at(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(relay, "deliver_codex") as deliver:
            path = self.message(directory, "2020-01-01T00:00:00+00:00", next_delivery_at=later)
            self.assertFalse(relay.dispatch_one(path))
            deliver.assert_not_called()

    def test_hop_limit_stops_and_notifies_sender(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(relay, "deliver_codex") as deliver, \
             mock.patch.object(relay, "notify_sender") as notify, \
             mock.patch.object(relay, "call", return_value=completed()) as call:
            path = self.message(directory, "2020-01-01T00:00:00+00:00", hops=relay.MAX_HOPS)
            self.assertTrue(relay.dispatch_one(path))
            deliver.assert_not_called()
            notify.assert_called_once()
            self.assertIn("--failed", call.call_args.args)

    def test_claude_target_is_pushed_to_the_channel_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            sock_path = Path(directory) / "docs.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(sock_path))
            server.listen(1)
            received = []

            def read_one():
                conn, _ = server.accept()
                with conn:
                    received.append(json.loads(conn.makefile("r", encoding="utf-8").readline()))
            worker = threading.Thread(target=read_one)
            worker.start()
            with mock.patch.object(relay, "BRIDGE", Path(directory)):
                relay.deliver_claude({"from": "codex:slot-x", "to": "claude:docs", "body": "done", "hops": 1})
            worker.join(timeout=2)
            server.close()
        self.assertEqual(received[0]["from"], "codex:slot-x")
        self.assertIn("done", received[0]["text"])
        self.assertEqual(received[0]["hops"], 1)


if __name__ == "__main__":
    unittest.main()
