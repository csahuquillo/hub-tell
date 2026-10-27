#!/usr/bin/env python3
"""Unit tests for hub-codex: turns, queueing, replies, identity binding and slot lifecycle.

The Codex daemon is always replaced by a fake; nothing here needs a network or a real Codex.
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hub_codex", ROOT / "hub-codex.py")
hub_codex = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hub_codex)


class ThreadBusy(Exception):
    pass


def fake_daemon_module(calls, *, reply="RESULT: COMPLETED\nSLOT_READY", busy_on_turn=False, thread=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append(("connect", kwargs))

        def initialize(self):
            calls.append(("initialize",))

        def create_thread(self, name, cwd, sandbox):
            calls.append(("create", name, cwd, sandbox))
            return "thread-visible"

        def is_busy(self, thread_id):
            return False

        def run_turn(self, thread_id, text, timeout):
            calls.append(("turn", thread_id, text, timeout))
            if busy_on_turn:
                raise ThreadBusy(thread_id)
            return reply

        def read_thread(self, thread_id):
            calls.append(("read", thread_id))
            return {"thread": thread or {}}

        def archive_thread(self, thread_id):
            calls.append(("archive", thread_id))

        def close(self):
            calls.append(("close",))

    return types.SimpleNamespace(CodexDaemon=FakeClient, ThreadBusy=ThreadBusy)


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patches = [
            mock.patch.object(hub_codex, "STATE_ROOT", self.root / "state"),
            mock.patch.object(hub_codex, "BRIDGE_STATE", self.root / "bridge"),
            mock.patch.object(hub_codex, "HUB_BUS_COMMAND", self.root / "no-bus"),
            mock.patch.object(hub_codex, "HUB_TELL_COMMAND", self.root / "no-tell"),
            mock.patch.object(hub_codex, "CODEX_HOME", self.root / "codex-home"),
            mock.patch.object(hub_codex, "DAEMON_CWD", ""),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_slot(self, name, **state):
        slot = hub_codex.slot_dir(name)
        (slot / "events").mkdir(parents=True)
        (slot / "messages").mkdir()
        value = {"name": name, "workspace": str(ROOT), "status": "idle", "turn_count": 0,
                 "sandbox": "read-only", "transport": "daemon", **state}
        hub_codex.save_state(name, value)
        return slot

    def args(self, **values):
        return types.SimpleNamespace(**values)


class TurnTests(StateTestCase):
    def test_open_creates_a_visible_daemon_thread(self):
        calls = []
        args = self.args(name="slot-reviewer", workspace=str(ROOT), sandbox="read-only",
                         timeout_seconds=900, created_by="claude:docs")
        with mock.patch.object(hub_codex, "daemon_module", return_value=fake_daemon_module(calls)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_codex.command_open(args), 0)
        state = hub_codex.load_state("slot-reviewer")
        self.assertEqual(state["transport"], "daemon")
        self.assertEqual(state["thread_id"], "thread-visible")
        self.assertEqual(state["turn_count"], 1)
        create = next(c for c in calls if c[0] == "create")
        self.assertEqual(create, ("create", "slot-reviewer", str(ROOT.resolve()), "read-only"))
        turn = next(c for c in calls if c[0] == "turn")
        self.assertTrue(turn[2].startswith("[hub · from hub · to codex:slot-reviewer]"))
        self.assertIn("codex:slot-reviewer", turn[2])

    def test_daemon_cwd_overrides_thread_project_folder(self):
        calls = []
        self.make_slot("slot-a")
        with mock.patch.object(hub_codex, "DAEMON_CWD", "/srv/projects/hub"), \
             mock.patch.object(hub_codex, "daemon_module", return_value=fake_daemon_module(calls)), \
             contextlib.redirect_stdout(io.StringIO()):
            hub_codex.run_turn("slot-a", hub_codex.load_state("slot-a"), "hi", None)
        self.assertEqual(next(c for c in calls if c[0] == "create")[2], "/srv/projects/hub")

    def test_busy_thread_defers_instead_of_starting_a_turn(self):
        runs = []
        state = {"name": "slot-busy", "thread_id": "thread-x", "turn_count": 3}
        self.make_slot("slot-busy", thread_id="thread-x", turn_count=3)
        with mock.patch.object(hub_codex, "daemon_thread_busy", return_value=True), \
             mock.patch.object(hub_codex.subprocess, "run", side_effect=lambda cmd, **kw: runs.append(cmd)):
            self.assertEqual(hub_codex.run_turn("slot-busy", state, "hello", "claude:api", True, "1793-abc", 0), 0)
        self.assertEqual(runs[0][1:4], ["defer", "1793-abc", "--by"])
        self.assertEqual(state["turn_count"], 3)
        self.assertNotEqual(state.get("status"), "working")

    def test_thread_busy_race_during_turn_also_defers(self):
        calls, runs = [], []
        self.make_slot("slot-race", thread_id="thread-x")
        state = hub_codex.load_state("slot-race")
        with mock.patch.object(hub_codex, "daemon_thread_busy", return_value=False), \
             mock.patch.object(hub_codex, "daemon_module", return_value=fake_daemon_module(calls, busy_on_turn=True)), \
             mock.patch.object(hub_codex.subprocess, "run", side_effect=lambda cmd, **kw: runs.append(cmd)):
            self.assertEqual(hub_codex.run_turn("slot-race", state, "hi", "claude:api", True, "req-9", 0), 0)
        self.assertTrue(any(cmd[1] == "defer" for cmd in runs))
        stored = hub_codex.load_state("slot-race")
        self.assertEqual(stored["status"], "idle")
        self.assertNotIn("active_turn", stored)

    def test_hop_limit_stops_before_starting_a_turn(self):
        sent = []
        with mock.patch.object(hub_codex, "update_bus"), \
             mock.patch.object(hub_codex, "daemon_module", side_effect=AssertionError("no daemon")), \
             mock.patch.object(hub_codex, "deliver_reply", side_effect=lambda *a, **k: sent.append(a)):
            result = hub_codex.run_turn("slot-loop", {}, "hi", "claude:api", True, "request", hub_codex.MAX_HOPS)
        self.assertEqual(result, 1)
        self.assertEqual(len(sent), 1)

    def test_failed_turn_is_recorded_and_reported(self):
        self.make_slot("slot-f", thread_id="thread-x")
        module = fake_daemon_module([])

        def boom(*args, **kwargs):
            raise RuntimeError("daemon down")
        module.CodexDaemon.run_turn = boom
        replies = []
        with mock.patch.object(hub_codex, "daemon_thread_busy", return_value=False), \
             mock.patch.object(hub_codex, "daemon_module", return_value=module), \
             mock.patch.object(hub_codex, "deliver_reply", side_effect=lambda *a, **k: replies.append((a, k))):
            with self.assertRaises(RuntimeError):
                hub_codex.run_turn("slot-f", hub_codex.load_state("slot-f"), "hi", "claude:api", True, None, 0)
        state = hub_codex.load_state("slot-f")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(replies[0][1]["outcome"], "failed")

    def test_dispatch_starts_a_detached_worker_with_hops_and_request_id(self):
        self.make_slot("slot-dispatch")
        args = self.args(name="dispatch", message="do it", sender="claude:api", no_reply=False,
                         request_id="req-1", hops=2)
        with mock.patch.object(hub_codex.subprocess, "Popen") as popen, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_codex.command_dispatch(args), 0)
        command = popen.call_args.args[0]
        self.assertIn("slot-dispatch", command)
        self.assertIn("req-1", command)
        self.assertEqual(command[command.index("--hops") + 1], "2")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_dispatch_to_unknown_or_closed_slot_fails_synchronously(self):
        self.make_slot("slot-closed", status="closed")
        for name in ("ghost", "closed"):
            args = self.args(name=name, message="x", sender="claude:api", no_reply=False, request_id=None, hops=0)
            with mock.patch.object(hub_codex.subprocess, "Popen") as popen, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    hub_codex.command_dispatch(args)
            popen.assert_not_called()

    def test_raw_thread_ids_cannot_send(self):
        self.make_slot("slot-a")
        args = self.args(name="a", message="x", sender="codex:thread-0123abcd", no_reply=False,
                         request_id=None, hops=0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hub_codex.command_dispatch(args)

    def test_drain_recovers_processing_queue_and_preserves_order(self):
        self.make_slot("slot-drain")
        pending = hub_codex.queue_path("slot-drain").with_suffix(".processing")
        pending.write_text(json.dumps({"prompt": "first"}) + "\n", encoding="utf-8")
        hub_codex.queue_path("slot-drain").write_text(json.dumps({"prompt": "second"}) + "\n", encoding="utf-8")
        seen = []
        with mock.patch.object(hub_codex, "run_turn", side_effect=lambda _n, _s, prompt, *a: seen.append(prompt) or 0):
            self.assertEqual(hub_codex.drain("slot-drain", hub_codex.load_state("slot-drain")), 0)
        self.assertEqual(seen, ["first", "second"])
        self.assertFalse(pending.exists())

    def test_terminal_outcome_uses_explicit_contract_only(self):
        self.assertEqual(hub_codex.terminal_outcome("RESULT: BLOCKED\nmissing access"), "blocked")
        self.assertEqual(hub_codex.terminal_outcome("I was blocked for a while, now done"), "completed")

    def test_redaction_of_tokens_and_inline_credentials(self):
        text = hub_codex.redact("token=abc123 and key " + "gh" + "p_" + "A" * 30)
        self.assertNotIn("abc123", text)
        self.assertNotIn("A" * 30, text)
        self.assertIn("[REDACTED]", text)

    def test_preamble_file_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("Custom rules for {slot}.")
        self.addCleanup(os.unlink, handle.name)
        with mock.patch.object(hub_codex, "PREAMBLE_FILE", handle.name):
            rendered = hub_codex.render_prompt("slot-x", "claude:api", "task")
        self.assertIn("Custom rules for slot-x.", rendered)
        self.assertTrue(rendered.endswith("MESSAGE:\ntask"))


class ReplyTests(StateTestCase):
    def test_reply_is_pushed_as_json_line_to_the_claude_channel(self):
        bridge = self.root / "bridge"
        bridge.mkdir()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(bridge / "api.sock"))
        server.listen(1)
        received = []

        def read_message():
            conn, _ = server.accept()
            with conn:
                received.append(json.loads(conn.makefile("r", encoding="utf-8").readline()))
        worker = threading.Thread(target=read_message)
        worker.start()
        hub_codex.deliver_reply("claude:api", "slot-worker", "done", request_id="r1", hops=2)
        worker.join(timeout=2)
        server.close()
        self.assertEqual(received[0]["from"], "codex:slot-worker")
        self.assertIn("[hub · from codex:slot-worker · reply]", received[0]["text"])
        self.assertIn("request=r1", received[0]["text"])
        self.assertEqual(received[0]["hops"], 3)

    def test_reply_falls_back_to_hub_tell_without_channel(self):
        tell = self.root / "hub-tell"
        tell.write_text("#!/bin/sh\n")
        tell.chmod(0o755)
        with mock.patch.object(hub_codex, "HUB_TELL_COMMAND", tell), \
             mock.patch.object(hub_codex.subprocess, "run") as run:
            hub_codex.deliver_reply("claude:api", "slot-worker", "done")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], [str(tell), "api"])
        self.assertEqual(run.call_args.kwargs["env"]["HUB_TELL_FROM"], "codex:slot-worker")

    def test_codex_to_codex_reply_is_queued_without_bouncing(self):
        with mock.patch.object(hub_codex.subprocess, "run") as run:
            hub_codex.deliver_reply("codex:slot-caller", "slot-worker", "done", hops=1)
        command = run.call_args.args[0]
        self.assertIn("--no-reply", command)
        self.assertEqual(command[command.index("dispatch") + 1], "slot-caller")

    def test_host_senders_get_no_reply(self):
        with mock.patch.object(hub_codex.subprocess, "run") as run:
            hub_codex.deliver_reply("host:admin-shell", "slot-worker", "done")
        run.assert_not_called()


class IdentityTests(StateTestCase):
    def make_db(self, rows, with_source=False):
        db = self.root / "codex-home" / "state_5.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db)
        if with_source:
            connection.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, thread_source TEXT)")
            connection.executemany("INSERT INTO threads VALUES (?, ?, ?, NULL)", rows)
        else:
            connection.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT)")
            connection.executemany("INSERT INTO threads VALUES (?, ?, ?)", rows)
        connection.commit()
        connection.close()

    def resolve(self, thread_id):
        args = self.args(thread_id=thread_id, workspace=str(ROOT), sandbox="read-only", timeout_seconds=900)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(hub_codex.command_resolve_thread_local(args), 0)
        return output.getvalue().strip()

    def test_unknown_thread_is_auto_bound_to_a_stable_slot_from_its_title(self):
        self.make_db([("full-thread-id-0001", "Audit vendor list ES", str(ROOT))])
        self.assertEqual(self.resolve("full-thread-id-0001"), "codex:slot-audit-vendor-list-es")
        self.assertEqual(self.resolve("full-thread-id-0001"), "codex:slot-audit-vendor-list-es")
        state = hub_codex.load_state("slot-audit-vendor-list-es")
        self.assertEqual(state["thread_id"], "full-thread-id-0001")
        self.assertFalse(state["archive_on_close"])

    def test_unique_prefix_resolves_and_missing_cwd_falls_back_to_workspace(self):
        self.make_db([("0a1b2c3d-1111-7222-8333-444455556666", "Review the release", "/nonexistent/dir")],
                     with_source=True)
        self.assertEqual(self.resolve("0a1b2c3d"), "codex:slot-review-the-release")
        state = hub_codex.load_state("slot-review-the-release")
        self.assertEqual(state["thread_id"], "0a1b2c3d-1111-7222-8333-444455556666")
        self.assertEqual(state["workspace"], str(ROOT.resolve()))

    def test_known_thread_resolves_without_touching_codex(self):
        self.make_slot("slot-known", thread_id="known-thread-id")
        with mock.patch.object(hub_codex, "thread_metadata", side_effect=AssertionError("no lookup")):
            self.assertEqual(self.resolve("known-thread-id"), "codex:slot-known")

    def test_daemon_is_used_when_no_local_registry_exists(self):
        calls = []
        module = fake_daemon_module(calls, thread={"id": "daemon-thread-01", "name": "Docs helper"})
        with mock.patch.object(hub_codex, "daemon_module", return_value=module):
            self.assertEqual(self.resolve("daemon-thread-01"), "codex:slot-docs-helper")
        self.assertIn(("read", "daemon-thread-01"), calls)

    def test_archived_threads_are_not_adopted(self):
        module = fake_daemon_module([], thread={"id": "archived-thread", "name": "Old", "archived": True})
        args = self.args(thread_id="archived-thread", workspace=str(ROOT), sandbox="read-only", timeout_seconds=900)
        with mock.patch.object(hub_codex, "daemon_module", return_value=module), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hub_codex.command_resolve_thread_local(args)

    def test_claim_goes_through_the_broker_with_the_thread_id(self):
        args = self.args(operation="acquire", resource="repo:demo", purpose="change")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "full-thread-id"}), \
             mock.patch.object(hub_codex, "broker_request", return_value={"ok": True, "result": "ACQUIRED"}) as broker, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_codex.command_claim(args), 0)
        request = broker.call_args.args[0]
        self.assertEqual((request["thread_id"], request["operation"], request["purpose"]),
                         ("full-thread-id", "acquire", "change"))
        self.assertNotIn("identity", request)

    def test_resolve_thread_falls_back_to_local_without_broker(self):
        self.make_slot("slot-known", thread_id="known-thread-id")
        args = self.args(thread_id="known-thread-id", workspace=str(ROOT), sandbox="read-only", timeout_seconds=900)
        output = io.StringIO()
        with mock.patch.object(hub_codex, "CLAIM_SOCKET", self.root / "missing.sock"), \
             contextlib.redirect_stdout(output):
            self.assertEqual(hub_codex.command_resolve_thread(args), 0)
        self.assertEqual(output.getvalue().strip(), "codex:slot-known")


class LifecycleTests(StateTestCase):
    def test_short_names_and_codex_prefix_resolve_to_the_slot(self):
        self.make_slot("slot-api")
        for name in ("api", "slot-api", "codex:api", "codex:slot-api"):
            self.assertEqual(hub_codex.resolve_existing(name), "slot-api")

    def test_rename_moves_only_logical_state(self):
        self.make_slot("slot-old", thread_id="thread-visible")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_codex.command_rename(self.args(old_name="slot-old", new_name="slot-new")), 0)
        self.assertFalse(hub_codex.state_path("slot-old").exists())
        renamed = hub_codex.load_state("slot-new")
        self.assertEqual((renamed["name"], renamed["thread_id"]), ("slot-new", "thread-visible"))

    def test_read_never_presents_a_previous_result_as_current(self):
        slot = self.make_slot("slot-working", status="working", active_turn={"sequence": 2},
                              last_terminal_result={"sequence": 1})
        previous = slot / "messages" / "0001.md"
        previous.write_text("old answer", encoding="utf-8")
        state = hub_codex.load_state("slot-working")
        state["last_message"] = str(previous)
        hub_codex.save_state("slot-working", state)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hub_codex.command_read(self.args(name="slot-working"))
        self.assertIn("still working on turn 2", output.getvalue())
        self.assertNotIn("old answer", output.getvalue())

    def test_recover_requeues_claimed_messages_and_keeps_audit_copy(self):
        slot = self.make_slot("slot-recover", active_turn={"sequence": 7, "request_id": "r", "sender": "claude:api"})
        pending = hub_codex.queue_path("slot-recover").with_suffix(".processing")
        pending.write_text(json.dumps({"prompt": "keep me"}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_codex.command_recover(self.args(name="slot-recover")), 0)
        state = hub_codex.load_state("slot-recover")
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["recovered_queue_messages"], 1)
        self.assertNotIn("active_turn", state)
        self.assertIn("keep me", hub_codex.queue_path("slot-recover").read_text())
        self.assertEqual(len(list(slot.glob("queue.interrupted.*.jsonl"))), 1)

    def test_close_archives_thread_but_not_for_adopted_slots(self):
        calls = []
        self.make_slot("slot-own", thread_id="t-own", archive_on_close=True)
        self.make_slot("slot-adopted", thread_id="t-adopted", archive_on_close=False)
        with mock.patch.object(hub_codex, "daemon_module", return_value=fake_daemon_module(calls)), \
             contextlib.redirect_stdout(io.StringIO()):
            hub_codex.command_close(self.args(name="slot-own"))
            hub_codex.command_close(self.args(name="slot-adopted"))
        self.assertIn(("archive", "t-own"), calls)
        self.assertNotIn(("archive", "t-adopted"), calls)
        self.assertEqual(hub_codex.load_state("slot-adopted")["status"], "closed")

    def test_adopt_refuses_a_thread_already_bound(self):
        self.make_slot("slot-a", thread_id="thread-shared-01")
        args = self.args(name="slot-b", thread_id="thread-shared-01", workspace=str(ROOT), sandbox="read-only",
                         timeout_seconds=900, created_by="host", archive_on_close=False)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hub_codex.command_adopt(args)

    def test_state_audit_trail_records_transitions(self):
        self.make_slot("slot-audit")
        state = hub_codex.load_state("slot-audit")
        hub_codex.record_state_change("slot-audit", state, "test.event", actor="tests", details={"k": "v"})
        line = json.loads(hub_codex.state_event_path("slot-audit").read_text().splitlines()[-1])
        self.assertEqual((line["event"], line["actor"], line["revision"]), ("test.event", "tests", 1))


if __name__ == "__main__":
    unittest.main()
