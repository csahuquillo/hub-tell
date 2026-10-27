#!/usr/bin/env python3
"""Deliver queued hub-bus envelopes to Claude channels and Codex slots.

Normally the sender delivers its own message right away (``hub-tell`` and the
``ask_codex`` channel tool both register an envelope, dispatch it and mark it
delivered within seconds). The relay only handles what nobody dispatched:
envelopes written directly with ``hub-bus send``, deferred messages for busy
Codex threads, and retries after a failed delivery.

It also serves a small control socket used as a broker by sandboxed Codex
turns (identity resolution and resource claims), because those turns cannot
write the shared state themselves.

Run it as a long-lived user service (see systemd/hub-bus-relay.service) or
once with ``--once``.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import fcntl
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HUB_BUS_STATE", Path.home() / ".local/state/hub-bus"))
MESSAGES = ROOT / "messages"
BUS = Path(os.environ.get("HUB_BUS_COMMAND", HERE / "hub-bus"))
CODEX = Path(os.environ.get("HUB_CODEX_COMMAND", HERE / "hub-codex"))
TELL = Path(os.environ.get("HUB_TELL_COMMAND", HERE / "hub-tell"))
BRIDGE = Path(os.environ.get("HUB_BRIDGE_STATE", Path.home() / ".local/state/hub-bridge"))
CONTROL = Path(os.environ.get("HUB_CLAIM_SOCKET", ROOT / "relay.sock"))
# Optional reservation backend. It must accept:
#   <cmd> acquire <resource> --purpose <text> --from <identity>
#   <cmd> release <resource> --from <identity>
CLAIM = os.environ.get("HUB_CLAIM_COMMAND", "")
MAX_ATTEMPTS = int(os.environ.get("HUB_BUS_MAX_ATTEMPTS", "5"))
MAX_HOPS = int(os.environ.get("HUB_MAX_HOPS", "6"))
# Senders register, dispatch and mark their own envelopes as delivered within
# a few seconds. Without this grace period the relay and the sender would both
# start a turn and every message would run twice.
GRACE_SECONDS = int(os.environ.get("HUB_BUS_RELAY_GRACE", "15"))
SWEEP_SECONDS = 10
CONTROL_READY = threading.Event()


def call(*args: str, env=None, timeout=45) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(args[0]), *args[1:]], text=True, capture_output=True,
                          check=False, env=env, timeout=timeout)


def deliver_claude(message: dict) -> None:
    """Push to the slot's hub-bridge channel socket; fall back to hub-tell (tmux)."""
    target = message["to"].split(":", 1)[1]
    path = BRIDGE / f"{target}.sock"
    envelope = {"from": message["from"],
                "text": f"[hub · from {message['from']} · to {message['to']}]\n{message['body']}",
                "hops": int(message.get("hops", 0))}
    if path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(str(path))
                client.sendall((json.dumps(envelope, ensure_ascii=False) + "\n").encode())
            return
        except OSError:
            pass
    if not TELL.exists():
        raise RuntimeError("no channel socket for the slot and no hub-tell fallback")
    env = os.environ.copy()
    env["HUB_TELL_FROM"] = message["from"]
    result = call(str(TELL), target, message["body"], env=env)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "hub-tell could not deliver")


def deliver_codex(message: dict) -> None:
    target = message["to"].split(":", 1)[1]
    if target.startswith("thread-"):
        # Reply to a thread that signed with its raw id: bind it to a slot first.
        resolved = call(str(CODEX), "resolve-thread-local", target[len("thread-"):], timeout=30)
        if resolved.returncode or not resolved.stdout.strip().startswith("codex:slot-"):
            raise RuntimeError(resolved.stderr.strip() or f"could not bind {target} to a slot")
        target = resolved.stdout.strip().split(":", 1)[1]
    result = call(str(CODEX), "dispatch", target, message["body"], "--from", message["from"],
                  "--request-id", message["id"], "--hops", str(int(message.get("hops", 0))))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "hub-codex dispatch failed")


def notify_sender(message: dict, text: str) -> None:
    runtime, sep, name = message.get("from", "").partition(":")
    if not sep or runtime not in {"claude", "codex"}:
        return
    if runtime == "codex":
        call(str(CODEX), "dispatch", name, text, "--from", "host:hub-bus-relay", "--no-reply")
        return
    try:
        deliver_claude({"from": "host:hub-bus-relay", "to": message["from"],
                        "body": text, "hops": message.get("hops", 0)})
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass


def handle_control(request: dict) -> dict:
    """Broker for sandboxed Codex turns.

    The caller's identity is never taken from the request: it is derived from
    CODEX_THREAD_ID via hub-codex, so a turn cannot claim on behalf of
    another slot.
    """
    operation = request.get("operation")
    thread_id = str(request.get("thread_id", ""))
    if not thread_id:
        return {"ok": False, "result": "missing CODEX_THREAD_ID; cannot determine the slot identity"}
    resolved = call(str(CODEX), "resolve-thread-local", thread_id, timeout=15)
    if resolved.returncode:
        return {"ok": False, "result": resolved.stderr.strip() or "could not resolve or bind the Codex thread"}
    lines = resolved.stdout.strip().splitlines()
    identity = lines[-1] if lines else ""
    if not identity.startswith("codex:slot-"):
        return {"ok": False, "result": "hub-codex did not return a slot-bound identity"}
    if operation == "resolve-thread":
        return {"ok": True, "identity": identity}
    if operation not in {"acquire", "release"}:
        return {"ok": False, "result": "unknown broker operation"}
    if not CLAIM:
        return {"ok": False, "identity": identity,
                "result": "no claim backend configured on this host (set HUB_CLAIM_COMMAND for the relay)"}
    resource = str(request.get("resource", ""))
    args = [CLAIM, operation, resource]
    if operation == "acquire":
        purpose = str(request.get("purpose", "")).strip()
        if not purpose:
            return {"ok": False, "result": "acquire requires --purpose"}
        args.extend(["--purpose", purpose])
    args.extend(["--from", identity])
    result = call(*args, timeout=20)
    text = result.stdout.strip() or result.stderr.strip() or "the claim backend returned no output"
    return {"ok": result.returncode == 0, "identity": identity, "result": text}


def serve_control() -> None:
    CONTROL.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONTROL.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(CONTROL))
        os.chmod(CONTROL, 0o600)
        server.listen(16)
        CONTROL_READY.set()
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    request = json.loads(connection.makefile("r", encoding="utf-8").readline())
                    response = handle_control(request)
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    response = {"ok": False, "result": str(exc)}
                connection.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())


def actor(message: dict) -> str:
    """Who records the delivery in the bus: the target, or the sender for raw thread ids."""
    return message["from"] if message.get("to", "").startswith("codex:thread-") else message["to"]


def dispatch_one(path: Path) -> bool:
    try:
        message = json.loads(path.read_text(encoding="utf-8"))
        if message.get("status") != "queued" or message.get("delivered_at"):
            return False
        created = dt.datetime.fromisoformat(str(message.get("created_at", "")).replace("Z", "+00:00"))
        if (dt.datetime.now(dt.timezone.utc) - created).total_seconds() < GRACE_SECONDS:
            return False
        retry_at = message.get("next_delivery_at")
        if retry_at and dt.datetime.fromisoformat(retry_at) > dt.datetime.now(dt.timezone.utc):
            return False
        if int(message.get("hops", 0)) >= MAX_HOPS:
            why = f"[hub] Automatic conversation stopped: limit of {MAX_HOPS} hops reached."
            notify_sender(message, why)
            call(str(BUS), "deliver", message["id"], "--by", actor(message), "--failed",
                 "--max-attempts", "1", "--result", why)
            return True
        runtime = message["to"].split(":", 1)[0]
        if runtime == "claude":
            deliver_claude(message)
        elif runtime == "codex":
            deliver_codex(message)
        else:
            raise RuntimeError(f"unsupported target: {message.get('to')}")
        result = call(str(BUS), "deliver", message["id"], "--by", actor(message))
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "could not mark the message as delivered")
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError) as exc:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("status") != "queued" or current.get("delivered_at"):
                return False
            result = call(str(BUS), "deliver", current["id"], "--by", actor(current), "--failed",
                          "--max-attempts", str(MAX_ATTEMPTS), "--result", str(exc)[:1000])
            updated = json.loads(result.stdout) if result.returncode == 0 and result.stdout else {}
            if updated.get("status") == "failed":
                notify_sender(current, f"[hub] Could not deliver to {current.get('to')} "
                                       f"after {MAX_ATTEMPTS} attempts: {exc}")
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        return False


def scan() -> int:
    if not MESSAGES.exists():
        return 0
    return sum(int(dispatch_one(path)) for path in sorted(MESSAGES.glob("*.json")))


def inotify_fd() -> int | None:
    """Watch the message directory with inotify (Linux); None means poll instead."""
    if not MESSAGES.exists():
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None
        # IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE
        wd = libc.inotify_add_watch(fd, os.fsencode(MESSAGES), 0x00000008 | 0x00000080 | 0x00000100)
        if wd < 0:
            os.close(fd)
            return None
        return fd
    except (AttributeError, OSError):
        return None


def main() -> int:
    once = "--once" in sys.argv[1:]
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    MESSAGES.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (ROOT / ".relay.lock").open("a+")
    os.chmod(lock.name, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("hub-bus-relay is already running", file=sys.stderr)
        return 1
    fd = inotify_fd()
    threading.Thread(target=serve_control, daemon=True).start()
    if not CONTROL_READY.wait(5):
        raise RuntimeError("could not open the relay control socket")
    try:
        scan()
        if once:
            return 0
        last_sweep = time.monotonic()
        while True:
            readable = []
            if fd is None:
                time.sleep(1)
            else:
                readable, _, _ = select.select([fd], [], [], SWEEP_SECONDS)
                if readable:
                    try:
                        os.read(fd, 65536)
                    except BlockingIOError:
                        pass
            # Periodic sweep: grace periods and retry backoffs expire without
            # any filesystem event.
            if fd is None or readable or time.monotonic() - last_sweep >= SWEEP_SECONDS:
                scan()
                last_sweep = time.monotonic()
    except KeyboardInterrupt:
        return 0
    finally:
        if fd is not None:
            os.close(fd)
        CONTROL.unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
