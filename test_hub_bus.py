#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
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

    def run_bus(self, *args):
        return subprocess.run([sys.executable, str(BUS), *args], env=self.env, text=True, capture_output=True, check=True)

    def test_send_ack_complete_and_reply_correlation(self):
        sent = self.run_bus("send", "codex:worker", "Comprueba el estado", "--from", "claude:maestro", "--type", "task")
        message_id = sent.stdout.strip()
        inbox = json.loads(self.run_bus("inbox", "codex:worker").stdout)
        self.assertEqual(inbox[0]["id"], message_id)
        self.run_bus("ack", message_id, "--by", "codex:worker")
        self.run_bus("complete", message_id, "--by", "codex:worker", "--result", "OK")
        stored = json.loads((Path(self.tmp.name) / "messages" / f"{message_id}.json").read_text())
        self.assertEqual(stored["status"], "completed")
        reply = self.run_bus("send", "claude:maestro", "Resultado recibido", "--from", "codex:worker", "--type", "result", "--reply-to", message_id)
        reply_id = reply.stdout.strip()
        self.assertNotEqual(message_id, reply_id)
        self.assertEqual(json.loads((Path(self.tmp.name) / "messages" / f"{reply_id}.json").read_text())["reply_to"], message_id)

    def test_rejects_untrusted_names_and_nul(self):
        bad = subprocess.run([sys.executable, str(BUS), "send", "../../tmp", "x", "--from", "codex:a"], env=self.env, text=True, capture_output=True)
        self.assertNotEqual(bad.returncode, 0)
        with self.assertRaises(SystemExit):
            hub_bus.validate_body("x\x00y")


if __name__ == "__main__":
    unittest.main()
