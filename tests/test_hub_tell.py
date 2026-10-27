#!/usr/bin/env python3
"""Safety tests for the hub-tell launcher, using a fake tmux, hub-codex and hub-bus."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB_TELL = ROOT / "hub-tell"
LOGGING_TMUX = """#!/bin/sh
printf '%s\\n' "$*" >> "$HUB_TELL_TEST_LOG"
case "$1" in
  capture-pane) cat "$FAKE_PANE" ;;
  display-message) [ -n "$FAKE_SESSION" ] && printf '%s\\n' "$FAKE_SESSION" ;;
esac
exit "${FAKE_TMUX_RC:-0}"
"""
RULE = "─" * 20
ESC = "\x1b"


class HubTellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "tmux.log"
        tmux = self.bin / "tmux"
        tmux.write_text(LOGGING_TMUX, encoding="utf-8")
        tmux.chmod(0o755)
        self.pane = self.root / "pane.txt"
        self.pane.write_text("", encoding="utf-8")

    def script(self, name, body):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_tell(self, *args, env=None):
        base = {key: value for key, value in os.environ.items()
                if key not in {"CODEX_THREAD_ID", "HUB_TELL_FROM", "TMUX", "TMUX_PANE", "HUB_CODEX_COMMAND",
                               "HUB_BUS_COMMAND", "HUB_CODEX_STATE"}}
        base.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.root),
            "HUB_TELL_TEST_LOG": str(self.log), "FAKE_PANE": str(self.pane), "FAKE_SESSION": "",
            "HUB_TELL_LOG": str(self.root / "hub-tell.log"),
            "HUB_CODEX_COMMAND": str(self.root / "missing-hub-codex"),
            "HUB_BUS_COMMAND": str(self.root / "missing-hub-bus"),
        })
        base.update(env or {})
        completed = subprocess.run([str(HUB_TELL), *args], text=True, capture_output=True, env=base)
        tmux_log = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return completed, tmux_log

    # -- argument and target safety ------------------------------------------------

    def test_empty_message_is_rejected_before_selecting_a_slot(self):
        completed, tmux_log = self.run_tell("api", "  ")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("empty message", completed.stderr)
        self.assertEqual(tmux_log, "")

    def test_prefix_target_is_not_accepted_as_a_different_slot(self):
        completed, tmux_log = self.run_tell("api", "result", env={"FAKE_TMUX_RC": "1"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exactly 'slot-api'", completed.stderr)
        self.assertEqual(tmux_log, "has-session -t =slot-api\n")

    def test_exact_session_and_pane_targets_are_used_for_delivery(self):
        completed, tmux_log = self.run_tell("--force", "Api", "result")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("has-session -t =slot-Api", tmux_log)
        self.assertIn("send-keys -t =slot-Api: -l", tmux_log)
        self.assertIn("send-keys -t =slot-Api: Enter", tmux_log)

    def test_multiline_message_is_collapsed_to_one_line(self):
        completed, tmux_log = self.run_tell("-f", "api", "line one\nline two")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("line one line two", tmux_log)

    # -- sender identity ---------------------------------------------------------

    def test_claude_sender_uses_its_own_pane_not_the_destination(self):
        completed, tmux_log = self.run_tell("-f", "docs", "result", env={
            "TMUX": "/tmp/tmux-test,1,0", "TMUX_PANE": "%source", "FAKE_SESSION": "slot-api"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("display-message -p -t %source #S", tmux_log)
        self.assertIn("-l [hub-tell from claude:api] result", tmux_log)

    def test_codex_thread_signs_as_its_bound_slot(self):
        codex = self.script("hub-codex", "#!/bin/sh\n[ \"$1\" = resolve-thread ] && echo codex:slot-reviewer\n")
        completed, tmux_log = self.run_tell("-f", "docs", "result", env={
            "TMUX": "/tmp/tmux-test,1,0", "TMUX_PANE": "%source", "FAKE_SESSION": "slot-api",
            "CODEX_THREAD_ID": "0199abcd-ef01-7000-8000-000000000000", "HUB_CODEX_COMMAND": str(codex)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[hub-tell from codex:slot-reviewer] result", tmux_log)

    def test_unresolvable_codex_thread_does_not_send(self):
        codex = self.script("hub-codex", "#!/bin/sh\nexit 1\n")
        completed, tmux_log = self.run_tell("-f", "docs", "result", env={
            "CODEX_THREAD_ID": "0199abcd-ef01-7000-8000-000000000000", "HUB_CODEX_COMMAND": str(codex)})
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("send-keys", tmux_log)

    def test_bare_hub_tell_from_label_is_kept_as_host_identity(self):
        completed, tmux_log = self.run_tell("-f", "docs", "ready", env={"HUB_TELL_FROM": "laptop"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[hub-tell from host:laptop] ready", tmux_log)

    def test_stale_codex_hub_tell_from_is_ignored(self):
        completed, tmux_log = self.run_tell("-f", "docs", "ready", env={
            "HUB_TELL_FROM": "codex:slot-gone", "HUB_CODEX_STATE": str(self.root / "codex-state")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("does not exist", completed.stderr)
        self.assertRegex(tmux_log, r"\[hub-tell from host:[^\]]+-shell\] ready")

    # -- compose box detection ---------------------------------------------------

    def write_pane(self, box_line, top=RULE):
        self.pane.write_text(f"previous output\n{top}\n{box_line}\n{RULE}\n  status line\n", encoding="utf-8")

    def test_dim_placeholder_is_not_a_draft(self):
        self.write_pane(f"❯ {ESC}[2mTry \"fix lint errors\"{ESC}[0m", top=f"{'─' * 8} slot-docs {'─' * 3}")
        completed, tmux_log = self.run_tell("docs", "hello")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("send-keys", tmux_log)

    def test_dim_suggestion_with_colour_change_inside_is_not_a_draft(self):
        self.write_pane(f"❯ {ESC}[2m{ESC}[39mPress up to edit queued messages{ESC}[0m")
        completed, _ = self.run_tell("docs", "hello")
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_real_draft_blocks_delivery(self):
        self.write_pane("❯ half-written reply to someone")
        completed, tmux_log = self.run_tell("docs", "hello")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("REAL unsent draft", completed.stderr)
        self.assertNotIn("send-keys", tmux_log)

    def test_missing_compose_box_aborts(self):
        self.pane.write_text("a menu is open\n", encoding="utf-8")
        completed, tmux_log = self.run_tell("docs", "hello")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("could not locate the compose box", completed.stderr)
        self.assertNotIn("send-keys", tmux_log)

    # -- Codex targets -----------------------------------------------------------

    def test_codex_dispatch_registers_and_marks_the_envelope_delivered(self):
        bus_log = self.root / "bus.log"
        bus = self.script("hub-bus", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BUS_TEST_LOG\"\n"
                                     "[ \"$1\" = send ] && echo req-42\nexit 0\n")
        codex = self.script("hub-codex", "#!/bin/sh\nprintf 'codex %s\\n' \"$*\" >> \"$BUS_TEST_LOG\"\nexit 0\n")
        completed, tmux_log = self.run_tell("codex:reviewer", "task text", env={
            "HUB_BUS_COMMAND": str(bus), "HUB_CODEX_COMMAND": str(codex),
            "BUS_TEST_LOG": str(bus_log), "HUB_TELL_FROM": "claude:api"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        rows = bus_log.read_text().splitlines()
        self.assertEqual(rows[0], "send codex:slot-reviewer task text --from claude:api --type task")
        self.assertEqual(rows[1], "codex dispatch reviewer task text --from claude:api --request-id req-42")
        self.assertEqual(rows[2], "deliver req-42 --by claude:api")
        self.assertEqual(tmux_log, "")

    def test_failed_codex_dispatch_fails_the_envelope_and_exits_non_zero(self):
        bus_log = self.root / "bus.log"
        bus = self.script("hub-bus", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BUS_TEST_LOG\"\n"
                                     "[ \"$1\" = send ] && echo req-7\nexit 0\n")
        codex = self.script("hub-codex", "#!/bin/sh\necho 'slot does not exist' >&2\nexit 2\n")
        completed, _ = self.run_tell("codex:ghost", "task", env={
            "HUB_BUS_COMMAND": str(bus), "HUB_CODEX_COMMAND": str(codex),
            "BUS_TEST_LOG": str(bus_log), "HUB_TELL_FROM": "claude:api"})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("NOT delivered", completed.stderr)
        self.assertTrue(any(row.startswith("fail req-7 --by claude:api") for row in bus_log.read_text().splitlines()))

    def test_force_is_rejected_for_codex_targets(self):
        completed, _ = self.run_tell("-f", "codex:reviewer", "task")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("--force only applies", completed.stderr)


if __name__ == "__main__":
    unittest.main()
