#!/usr/bin/env python3
"""Tests for the durable, correlated hub-bus envelope store."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUS = ROOT / "hub-bus.py"
SPEC = importlib.util.spec_from_file_location("hub_bus", BUS)
hub_bus = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hub_bus)


class HubBusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "HUB_BUS_STATE": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def run_bus(self, *args, check=True):
        return subprocess.run([sys.executable, str(BUS), *args], env=self.env, text=True,
                              capture_output=True, check=check)

    def stored(self, message_id):
        return json.loads((Path(self.tmp.name) / "messages" / f"{message_id}.json").read_text())

    def age(self, message_id, created_at="2020-01-01T00:00:00+00:00"):
        path = Path(self.tmp.name) / "messages" / f"{message_id}.json"
        value = json.loads(path.read_text())
        value["created_at"] = created_at
        path.write_text(json.dumps(value))

    def test_send_ack_complete_and_reply_correlation(self):
        message_id = self.run_bus("send", "codex:slot-worker", "Check the deployment",
                                  "--from", "claude:api", "--type", "task").stdout.strip()
        inbox = json.loads(self.run_bus("inbox", "codex:slot-worker").stdout)
        self.assertEqual(inbox[0]["id"], message_id)
        self.run_bus("ack", message_id, "--by", "codex:slot-worker")
        self.run_bus("complete", message_id, "--by", "codex:slot-worker", "--result", "OK")
        self.assertEqual(self.stored(message_id)["status"], "completed")
        reply_id = self.run_bus("send", "claude:api", "Result received", "--from", "codex:slot-worker",
                                "--type", "result", "--reply-to", message_id).stdout.strip()
        self.assertNotEqual(message_id, reply_id)
        self.assertEqual(self.stored(reply_id)["reply_to"], message_id)

    def test_finished_messages_cannot_change_and_strangers_cannot_update(self):
        message_id = self.run_bus("send", "codex:slot-worker", "x", "--from", "claude:api").stdout.strip()
        stranger = self.run_bus("ack", message_id, "--by", "claude:docs", check=False)
        self.assertNotEqual(stranger.returncode, 0)
        self.run_bus("fail", message_id, "--by", "codex:slot-worker", "--result", "no")
        again = self.run_bus("complete", message_id, "--by", "codex:slot-worker", check=False)
        self.assertNotEqual(again.returncode, 0)

    def test_rejects_untrusted_names_and_nul(self):
        bad = self.run_bus("send", "../../tmp", "x", "--from", "codex:a", check=False)
        self.assertNotEqual(bad.returncode, 0)
        with self.assertRaises(SystemExit):
            hub_bus.validate_body("x\x00y")
        # Raw thread ids are still valid inbox names.
        self.assertEqual(hub_bus.valid_name("codex:thread-01234567"), "codex:thread-01234567")
        self.assertEqual(hub_bus.valid_name("host:relay"), "host:relay")

    def test_deliver_sets_timestamp_attempts_and_hops(self):
        message_id = self.run_bus("send", "claude:api", "reply", "--from", "codex:slot-a",
                                  "--hops", "2").stdout.strip()
        stored = self.stored(message_id)
        self.assertIsNone(stored["delivered_at"])
        self.assertEqual(stored["hops"], 2)
        self.assertEqual(stored["delivery_attempts"], 0)
        self.run_bus("deliver", message_id, "--by", "claude:api")
        stored = self.stored(message_id)
        self.assertTrue(stored["delivered_at"])
        self.assertEqual(stored["delivery_attempts"], 1)

    def test_failed_delivery_backs_off_then_fails_after_max_attempts(self):
        message_id = self.run_bus("send", "codex:slot-a", "x", "--from", "claude:api").stdout.strip()
        self.run_bus("deliver", message_id, "--by", "codex:slot-a", "--failed", "--max-attempts", "2")
        stored = self.stored(message_id)
        self.assertEqual(stored["status"], "queued")
        self.assertTrue(stored["next_delivery_at"])
        self.run_bus("deliver", message_id, "--by", "codex:slot-a", "--failed", "--max-attempts", "2",
                     "--result", "unreachable")
        stored = self.stored(message_id)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["result"], "unreachable")
        self.assertNotIn("next_delivery_at", stored)

    def test_defer_requeues_without_counting_attempt(self):
        message_id = self.run_bus("send", "codex:slot-a", "wait", "--from", "claude:api").stdout.strip()
        self.run_bus("deliver", message_id, "--by", "codex:slot-a")
        self.run_bus("defer", message_id, "--by", "codex:slot-a", "--seconds", "60")
        stored = self.stored(message_id)
        self.assertEqual(stored["status"], "queued")
        self.assertIsNone(stored["delivered_at"])
        self.assertEqual(stored["delivery_attempts"], 1)
        self.assertTrue(stored["next_delivery_at"])

    def test_close_stale_never_touches_raw_thread_inboxes(self):
        message_id = self.run_bus("send", "codex:thread-0a1b2c3d", "inbox", "--from", "claude:api").stdout.strip()
        self.age(message_id)
        self.run_bus("close-stale", "--before", "2021-01-01", "--by", "host:admin")
        self.assertEqual(self.stored(message_id)["status"], "queued")

    def test_close_stale_closes_old_pending_and_keeps_audit_trail(self):
        old_id = self.run_bus("send", "codex:slot-a", "old", "--from", "host:admin").stdout.strip()
        new_id = self.run_bus("send", "codex:slot-a", "new", "--from", "host:admin").stdout.strip()
        self.age(old_id)
        result = json.loads(self.run_bus("close-stale", "--before", "2021-01-01T00:00:00Z",
                                         "--by", "host:admin").stdout)
        self.assertEqual(result["closed"], 1)
        closed = self.stored(old_id)
        self.assertEqual(closed["status"], "completed")
        self.assertEqual(closed["result"], "closed by cleanup (stale)")
        self.assertEqual(self.stored(new_id)["status"], "queued")
        self.assertIn("closed-stale", (Path(self.tmp.name) / "audit.jsonl").read_text())

    def test_state_is_private(self):
        message_id = self.run_bus("send", "codex:slot-a", "x", "--from", "claude:api").stdout.strip()
        path = Path(self.tmp.name) / "messages" / f"{message_id}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.tmp.name).stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
