#!/usr/bin/env python3
"""Persistent, local message bus for Claude and Codex slots.

The bus is deliberately transport-neutral: delivery is an adapter concern, while
this module owns the durable envelope, correlation IDs, acknowledgements and
audit trail. State is private (0700/0600) and messages are written atomically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("HUB_BUS_STATE", Path.home() / ".local/state/hub-bus"))
NAME_RE = re.compile(r"^(?:claude:|codex:)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_BODY = 32_000


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def error(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def valid_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        error("destino/origen inválido")
    return value


def valid_id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        error("id inválido")
    return value


def validate_body(value: str) -> str:
    if not value or len(value) > MAX_BODY or "\x00" in value:
        error(f"el mensaje debe tener entre 1 y {MAX_BODY} caracteres y no contener NUL")
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
        error(f"no se puede leer {path.name}: {exc}")
    if not isinstance(value, dict):
        error(f"formato inválido en {path.name}")
    return value


def audit(event: str, message: dict[str, Any]) -> None:
    with (ROOT / "audit.jsonl").open("a", encoding="utf-8") as handle:
        os.chmod(ROOT / "audit.jsonl", 0o600)
        handle.write(json.dumps({"at": stamp(), "event": event, "message": message}, ensure_ascii=False) + "\n")


def message_path(message_id: str) -> Path:
    return ROOT / "messages" / f"{valid_id(message_id)}.json"


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
        message = read_json(path)
        if message.get("status") in {"completed", "failed"}:
            error("el mensaje ya está terminado")
        if actor not in {message.get("to"), message.get("from")}:
            error("el agente no pertenece a este mensaje")
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Bus persistente y correlacionado Claude/Codex")
    sub = result.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="encola un mensaje")
    send.add_argument("target")
    send.add_argument("message")
    send.add_argument("--from", dest="source", required=True)
    send.add_argument("--type", choices=("task", "question", "result", "blocked", "handoff"), default="task")
    send.add_argument("--reply-to")
    send.set_defaults(func=command_send)
    inbox = sub.add_parser("inbox", help="muestra mensajes pendientes")
    inbox.add_argument("target")
    inbox.set_defaults(func=command_inbox)
    for command, status in (("ack", "received"), ("complete", "completed"), ("fail", "failed")):
        item = sub.add_parser(command)
        item.add_argument("message_id")
        item.add_argument("--by", dest="actor", required=True)
        if command != "ack":
            item.add_argument("--result")
        item.set_defaults(func=lambda args, status=status: update(args.message_id, status, args.actor, getattr(args, "result", None)))
    sub.add_parser("status", help="resumen de mensajes").set_defaults(func=command_status)
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    raise SystemExit(args.func(args))
