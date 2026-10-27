#!/usr/bin/env python3
"""Persistent, local message bus for Claude and Codex slots.

The bus is deliberately transport-neutral: delivery is an adapter concern
(``hub-tell``, the ``hub-bridge`` channel, ``hub-codex`` and ``hub-bus-relay``),
while this module owns the durable envelope, correlation IDs,
acknowledgements, delivery bookkeeping and the audit trail.

State is private (0700 directories, 0600 files) and every message is written
atomically under an exclusive lock.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("HUB_BUS_STATE", Path.home() / ".local/state/hub-bus"))
NAME_RE = re.compile(r"^(?:claude:|codex:|host:)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_BODY = 32_000
MESSAGE_TYPES = ("task", "question", "result", "blocked", "handoff")
TERMINAL = {"completed", "failed"}


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def error(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def valid_name(value: str) -> str:
    # ``codex:thread-<id>`` (a Codex thread that signed with its raw thread id
    # instead of a slot name) is still accepted so that its inbox can be read
    # and answered; the relay binds it to a slot before delivering.
    if not NAME_RE.fullmatch(value):
        error("invalid source/target name")
    return value


def valid_id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        error("invalid message id")
    return value


def validate_body(value: str) -> str:
    if not value or len(value) > MAX_BODY or "\x00" in value:
        error(f"the message must be 1-{MAX_BODY} characters long and contain no NUL bytes")
    return value


def lock_file() -> Path:
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)
    return ROOT / ".lock"


def locked(action):
    path = lock_file()
    with path.open("a+") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        return action()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"cannot read {path.name}: {exc}")
    if not isinstance(value, dict):
        error(f"invalid format in {path.name}")
    return value


def audit(event: str, message: dict[str, Any]) -> None:
    with (ROOT / "audit.jsonl").open("a", encoding="utf-8") as handle:
        os.chmod(ROOT / "audit.jsonl", 0o600)
        handle.write(json.dumps({"at": stamp(), "event": event, "message": message}, ensure_ascii=False) + "\n")


def message_path(message_id: str) -> Path:
    return ROOT / "messages" / f"{valid_id(message_id)}.json"


def load_owned(path: Path, actor: str) -> dict[str, Any]:
    """Load a non-terminal message that ``actor`` is a party to."""
    message = read_json(path)
    if message.get("status") in TERMINAL:
        error("the message is already finished")
    if actor not in {message.get("to"), message.get("from")}:
        error("the actor is not a party to this message")
    return message


def command_send(args: argparse.Namespace) -> int:
    source = valid_name(args.source)
    target = valid_name(args.target)
    body = validate_body(args.message)
    reply_to = valid_id(args.reply_to) if args.reply_to else None
    message_id = f"{int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)}-{secrets.token_hex(4)}"
    message = {
        "id": message_id,
        "from": source,
        "to": target,
        "type": args.type,
        "reply_to": reply_to,
        "body": body,
        "created_at": stamp(),
        "status": "queued",
        "delivered_at": None,
        "delivery_attempts": 0,
        "hops": max(0, int(getattr(args, "hops", 0))),
    }

    def action() -> None:
        write_json(message_path(message_id), message)
        audit("queued", message)

    locked(action)
    print(message_id)
    return 0


def command_inbox(args: argparse.Namespace) -> int:
    target = valid_name(args.target)
    rows = []
    directory = ROOT / "messages"
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        message = read_json(path)
        if message.get("to") == target and message.get("status") in {"queued", "received"}:
            rows.append(message)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def update(message_id: str, status: str, actor: str, body: str | None = None) -> int:
    actor = valid_name(actor)
    path = message_path(message_id)

    def action() -> None:
        message = load_owned(path, actor)
        message["status"] = status
        message["updated_at"] = stamp()
        message["updated_by"] = actor
        if body is not None:
            message["result"] = validate_body(body)
        write_json(path, message)
        audit(status, message)
        print(json.dumps(message, ensure_ascii=False, indent=2))

    locked(action)
    return 0


def command_status(args: argparse.Namespace) -> int:
    counts: dict[str, int] = {}
    for path in ROOT.glob("messages/*.json") if (ROOT / "messages").exists() else []:
        status = str(read_json(path).get("status", "invalid"))
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"state_root": str(ROOT), "messages": counts}, ensure_ascii=False, indent=2))
    return 0


def command_deliver(args: argparse.Namespace) -> int:
    """Record a delivery attempt (successful, or failed with backoff)."""
    actor = valid_name(args.actor)
    path = message_path(args.message_id)

    def action() -> None:
        message = load_owned(path, actor)
        message["delivery_attempts"] = int(message.get("delivery_attempts", 0)) + 1
        if args.failed:
            message["delivered_at"] = None
            if message["delivery_attempts"] >= args.max_attempts:
                message["status"] = "failed"
                message["result"] = args.result
                message.pop("next_delivery_at", None)
            else:
                delay = min(2 ** message["delivery_attempts"], 300)
                retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
                message["next_delivery_at"] = retry_at.isoformat(timespec="seconds")
        else:
            message["delivered_at"] = stamp()
            message.pop("next_delivery_at", None)
        message["updated_at"] = stamp()
        message["updated_by"] = actor
        write_json(path, message)
        audit("delivered" if not args.failed else "delivery-failed", message)
        print(json.dumps(message, ensure_ascii=False, indent=2))

    locked(action)
    return 0


def command_defer(args: argparse.Namespace) -> int:
    """Put a message back in the queue because its target is busy.

    Typical case: a Codex thread that already has a turn running (for example
    because a human is using it in Codex Desktop). This does not count as a
    failed delivery attempt; the relay retries after ``--seconds``.
    """
    actor = valid_name(args.actor)
    path = message_path(args.message_id)

    def action() -> None:
        message = load_owned(path, actor)
        retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=args.seconds)
        message.update(status="queued", delivered_at=None, updated_at=stamp(), updated_by=actor,
                       next_delivery_at=retry_at.isoformat(timespec="seconds"))
        write_json(path, message)
        audit("deferred", message)
        print(json.dumps(message, ensure_ascii=False, indent=2))

    locked(action)
    return 0


def parse_cutoff(value: str) -> dt.datetime:
    try:
        if "T" not in value:
            return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        error("invalid --before date (use YYYY-MM-DD or ISO-8601)")
    raise AssertionError("unreachable")


def command_close_stale(args: argparse.Namespace) -> int:
    """Close old queued/received messages without deleting their audit trail."""
    actor = valid_name(args.actor)
    cutoff = parse_cutoff(args.before)
    changed = 0

    def action() -> None:
        nonlocal changed
        directory = ROOT / "messages"
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            message = read_json(path)
            if message.get("status") not in {"queued", "received"}:
                continue
            try:
                created = dt.datetime.fromisoformat(str(message.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff:
                continue
            # Codex threads addressed by raw id (codex:thread-*) use the bus as
            # their inbox: a human may still read those messages from the
            # thread, so a bulk cleanup must never close them.
            if str(message.get("to", "")).startswith("codex:thread-"):
                continue
            message.update(status="completed", result=args.result,
                           updated_at=stamp(), updated_by=actor, closed_by=actor)
            write_json(path, message)
            audit("closed-stale", message)
            changed += 1
        print(json.dumps({"closed": changed, "before": args.before}, ensure_ascii=False))

    locked(action)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Persistent, correlated Claude/Codex message bus")
    sub = result.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="queue a message")
    send.add_argument("target")
    send.add_argument("message")
    send.add_argument("--from", dest="source", required=True)
    send.add_argument("--type", choices=MESSAGE_TYPES, default="task")
    send.add_argument("--reply-to")
    send.add_argument("--hops", type=int, default=0, help="anti-loop counter carried by automatic replies")
    send.set_defaults(func=command_send)

    inbox = sub.add_parser("inbox", help="show pending messages for a target")
    inbox.add_argument("target")
    inbox.set_defaults(func=command_inbox)

    for command, status in (("ack", "received"), ("complete", "completed"), ("fail", "failed")):
        item = sub.add_parser(command, help=f"mark a message as {status}")
        item.add_argument("message_id")
        item.add_argument("--by", dest="actor", required=True)
        if command != "ack":
            item.add_argument("--result")
        item.set_defaults(func=lambda args, status=status: update(
            args.message_id, status, args.actor, getattr(args, "result", None)))

    sub.add_parser("status", help="summary of messages by status").set_defaults(func=command_status)

    deliver = sub.add_parser("deliver", help="record that a message was handed to its runtime")
    deliver.add_argument("message_id")
    deliver.add_argument("--by", dest="actor", required=True)
    deliver.add_argument("--failed", action="store_true", help="record a failed attempt (exponential backoff)")
    deliver.add_argument("--max-attempts", type=int, default=5)
    deliver.add_argument("--result", default="could not be delivered after several attempts")
    deliver.set_defaults(func=command_deliver)

    defer = sub.add_parser("defer", help="requeue a message whose target is busy")
    defer.add_argument("message_id")
    defer.add_argument("--by", dest="actor", required=True)
    defer.add_argument("--seconds", type=int, default=60)
    defer.set_defaults(func=command_defer)

    stale = sub.add_parser("close-stale", help="close old pending messages, keeping the audit trail")
    stale.add_argument("--before", required=True)
    stale.add_argument("--by", dest="actor", required=True)
    stale.add_argument("--result", default="closed by cleanup (stale)")
    stale.set_defaults(func=command_close_stale)
    return result


if __name__ == "__main__":
    parsed = parser().parse_args()
    raise SystemExit(parsed.func(parsed))
