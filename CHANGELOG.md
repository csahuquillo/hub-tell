# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
follows [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-10-27

Dynamic Claude ↔ Codex messaging: questions to Codex become turns in
persistent threads visible in Codex Desktop, and answers are pushed back into
the asking Claude session without anyone polling or relaying by hand.

### Added

- **`hub-bridge` channel** (`bridge/hub_bridge_channel.py`): per-slot MCP stdio
  server for Claude Code declaring the experimental `claude/channel`
  capability. It listens on `~/.local/state/hub-bridge/<slot>.sock` (0600) and
  turns every JSON line written there into a `notifications/claude/channel`
  event, which arrives in the session as a new turn. Meta values are always
  strings (a numeric value makes the session drop the notification silently).
- Channel tools **`ask_codex`**, **`list_codex`** and **`open_codex`**: register
  a correlated bus envelope, dispatch a background Codex turn, mark the
  envelope delivered and return immediately; the answer arrives on the channel.
- **`hub-codex`** (`hub-codex`, `hub-codex.py`): Codex slot manager with
  `open`, `adopt`, `dispatch`, `send`/`tell`, `drain`, `list [--json]`,
  `status`, `read`, `close`, `purge`, `rename`, `recover`, `resolve-thread`,
  `claim acquire|release` and `relay`. Per-slot state with an append-only audit
  trail of state changes, a durable per-slot queue with crash recovery, and
  final answers stored per turn.
- **Codex App Server daemon client** (`bridge/codex_daemon.py`): standard-library
  JSON-RPC over WebSocket on the daemon's Unix socket, with masked client
  frames, ping/pong, fragmented messages, a 16 MiB message cap, no automatic
  resend of a possibly executed request, `thread/resume` with `excludeTurns`
  for cheap status checks, and a `ThreadBusy` exception for threads that
  already have a turn running.
- Threads created by `hub-codex open` live in the daemon and are therefore
  visible and usable in Codex Desktop; they are named after the slot and run
  with `approvalPolicy=never` and no network in the sandbox.
- **Replies routed back automatically**: when a turn finishes, its final
  message goes to the sender — to a Claude slot through its channel socket
  (falling back to `hub-tell`), to another Codex slot as a queued turn marked
  `--no-reply` so completions do not bounce forever.
- **`hub-bus-relay.py`** and `systemd/hub-bus-relay.service`: delivers
  envelopes nobody dispatched (15 s grace period so sender and relay never
  both start the turn), deferred envelopes and failed ones (exponential
  backoff, 5 attempts, then the sender is notified). Watches the queue with
  inotify and sweeps every 10 s.
- **Anti-loop hop counter**: carried by envelopes, channel messages and
  replies; automatic conversations stop at 6 hops (`HUB_MAX_HOPS`).
- **Identity binding for Codex threads**: `hub-codex resolve-thread` maps
  `CODEX_THREAD_ID` (full id or unique prefix) to a stable
  `codex:slot-<name>`, auto-adopting an unbound, non-archived thread under a
  name derived from its title.
- **Broker socket** in the relay for sandboxed Codex turns: identity
  resolution and claims (`hub-codex claim`), always deriving the caller from
  its thread id. Claims run an optional external backend
  (`HUB_CLAIM_COMMAND`).
- **Busy-thread deferral**: a message for a thread with a turn in progress is
  requeued on the bus (`hub-bus defer`) instead of stacking a second turn.
- `hub-bus` commands **`deliver`** (with `--failed`, `--max-attempts`),
  **`defer`** and **`close-stale`**; envelope fields `delivered_at`,
  `delivery_attempts`, `next_delivery_at` and `hops`; `host:` identities.
- **`slot-run`**: runs a Claude slot in tmux session `slot-<name>` with
  `--name slot-<name>`, a generated `--mcp-config` for `hub-bridge`,
  `--dangerously-load-development-channels server:hub-bridge`, deterministic
  `--resume` from `~/.claude/slot-sessions/<name>.session`, and a babysitter
  that answers the resume menu, the folder-trust dialog, the
  development-channel dialog and cancels the auto-mode setup wizard.
- **`slot-session-hook`**: `SessionStart` hook that records the current
  session id so a restart after `/clear` or compaction resumes the new
  conversation.
- systemd examples: `claude-rc@.service`, `codex-app-server.service`,
  `hub-bus-relay.service` and the `20-killmode.conf` drop-in.
- Configurable Codex turn preamble (`HUB_CODEX_PREAMBLE_FILE`) and explicit
  result contract (`RESULT: COMPLETED` / `RESULT: BLOCKED`).
- Basic secret redaction of persisted logs and forwarded answers.
- `AGENTS.md.example`, `CHANGELOG.md`, `docs/SECURITY.md`, and a test suite
  under `tests/` (bus, relay, daemon client against a fake App Server,
  channel, hub-codex, hub-tell against a fake tmux) that runs offline.

### Changed

- `hub-tell` supports **`codex:<slot>` targets**: it records a correlated
  envelope, dispatches through `hub-codex` (which validates the slot
  synchronously, so a typo fails instead of looking delivered) and marks the
  envelope delivered; on failure the envelope is failed.
- Sender identity is now resolved from the runtime and has a prefix:
  `claude:<name>` from the caller's **own** tmux pane (only when `$TMUX` is
  set), `codex:slot-<name>` from `CODEX_THREAD_ID`. An inherited
  `HUB_TELL_FROM` that contradicts the runtime identity is ignored;
  a `codex:slot-*` value naming a non-existent slot and raw `codex:thread-*`
  values are rejected; a bare label becomes `host:<label>`; otherwise the
  sender is `host:<user>-shell`.
- The compose-box guard ignores dimmed text (SGR 2): placeholders,
  continuation suggestions and "Press up to edit queued messages", even when
  a colour change appears inside the dim run. It also recognises the top
  border that carries the session name (`──── slot-x ─`).
- The log location can be set with `HUB_TELL_LOG`.
- `hub-bus` rejects updates to finished envelopes and by non-parties through
  one shared check; messages and help text are in English.
- README rewritten (architecture, installation, configuration, security,
  limitations, upgrade notes); `CLAUDE.md.example` updated for native
  `SendMessage`, `ask_codex` and the "information, not authorization" rule.
- `test_hub_bus.py` moved to `tests/test_hub_bus.py`.

### Fixed

- Empty messages (e.g. an unset `$MSG`) are rejected instead of delivering an
  empty turn.
- A sender outside a tmux client (cron, a script, Codex) no longer takes the
  identity of the tmux server's active session, i.e. another slot's name.
- `hub-tell api` could select `slot-api2` through tmux prefix matching; every
  tmux operation now uses the exact target.
- The compose-box guard no longer refuses to send when the box only shows a
  dimmed continuation suggestion (it previously accepted only the
  `Try "..."` placeholder, so it blocked most deliveries to idle sessions).
- A message containing newlines no longer submits half of its text.

## [1.x]

Untagged releases on `main` (commits `9a7e226` to `27bc681`).

### Added

- `hub-tell <slot> <message>`: types a message into the tmux session
  `slot-<slot>` as a user turn, with separate text and Enter keystrokes and a
  guard that refuses to write over an unsent draft.
- `HUB_TELL_FROM` override for senders outside a tmux session, and notes on
  the cross-machine bridging options that were evaluated.
- `hub-bus`: a durable, correlated message envelope store (send, inbox, ack,
  complete, fail, status) with private permissions, atomic writes, strict
  name validation and a 32,000-character limit, plus its tests.
- Documentation of Claude ↔ Codex coordination through `codex:` targets.
