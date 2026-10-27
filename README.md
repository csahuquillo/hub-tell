# hub-tell

Local messaging between long-lived [Claude Code](https://claude.com/claude-code)
sessions and Codex threads running on the same machine.

If you keep several Claude Code sessions ("slots") alive on one server — each
one its own project or automation, kept in `tmux` and reachable through
[Remote Control](https://docs.claude.com/en/docs/claude-code/remote-control) —
and also use Codex on that host, you eventually want them to say "I finished
X, can you check Y" to each other and get the answer back without you
copy-pasting between windows. This repository is the plumbing for that:

* **Claude ↔ Claude**: Claude Code's native `SendMessage` between named
  sessions (`slot-<name>`), with `hub-tell` as the tmux-based fallback.
* **Claude → Codex**: a per-slot MCP channel (`hub-bridge`) with an
  `ask_codex` tool. The question becomes a turn in a persistent Codex thread
  that is **visible in Codex Desktop**; the answer is pushed back into the
  Claude session on its own, waking it up.
* **Codex → anyone**: `hub-tell` from inside a Codex thread, signed with the
  thread's stable slot identity.
* A durable, correlated **message bus** (`hub-bus`) underneath, plus a relay
  that delivers whatever nobody delivered (busy threads, retries).

Everything is local: Unix sockets, tmux and files under `~/.local/state`. No
network service is opened and no credentials are added.

> Version 2.0 is a large extension of the original single-script `hub-tell`.
> `hub-tell <slot> "message"` still works the same way. See
> [Upgrading from v1](#upgrading-from-v1) and [CHANGELOG.md](CHANGELOG.md).

## Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuring Claude slots](#configuring-claude-slots)
- [Configuring Codex](#configuring-codex)
- [Usage](#usage)
- [Reference](#reference)
- [Security](#security)
- [Known limitations](#known-limitations)
- [Upgrading from v1](#upgrading-from-v1)
- [Tests](#tests)

## Architecture

```
  Claude slot "docs"                                   Claude slot "api"
  (tmux slot-docs, --name slot-docs)                   (tmux slot-api)
        |  ^                                                  ^
        |  |        native SendMessage / ListAgents           |
        |  +--------------------------------------------------+
        |           (fallback: hub-tell api "..." types into tmux)
        |
        | tool call: ask_codex(codex="reviewer", message="...")
        v
  hub-bridge channel  (MCP stdio server started by Claude Code,
        |              socket ~/.local/state/hub-bridge/docs.sock, 0600)
        | 1. hub-bus send  codex:slot-reviewer   -> durable envelope (id)
        | 2. hub-codex dispatch slot-reviewer     -> detached worker, returns at once
        | 3. hub-bus deliver <id>                 -> "handed over"
        v
  hub-codex send (background)
        |  JSON-RPC over WebSocket on a Unix socket
        v
  codex app-server daemon  ---->  thread "slot-reviewer"
        |                          (visible and usable in Codex Desktop)
        | final message of the turn
        v
  hub-codex deliver_reply ---- JSON line ----> docs.sock ----> notifications/claude/channel
                                                               (new turn in slot "docs")

  hub-bus-relay (user service)
     - delivers envelopes nobody dispatched (after a 15 s grace period),
       deferred ones (thread was busy) and failed ones (backoff, 5 attempts)
     - stops automatic ping-pong at 6 hops
     - broker socket for sandboxed Codex turns: identity + claims
```

### Identities

| Who | Identity | Address it as |
|---|---|---|
| Claude slot in tmux session `slot-docs` | `claude:docs` | `hub-tell docs ...`, native peer `slot-docs` |
| Codex slot | `codex:slot-reviewer` | `hub-tell codex:reviewer ...`, `ask_codex(codex="reviewer")` |
| Anything else (cron, a plain shell) | `host:<label>` | not addressable |

A sender's identity is derived from its runtime, not from what it claims:
inside a Codex thread from `CODEX_THREAD_ID` (bound to its slot by
`hub-codex resolve-thread`), inside a Claude slot from its own tmux pane.
`HUB_TELL_FROM` is only used when no runtime identity exists.

### Components

| File | Role |
|---|---|
| `hub-tell` | CLI: send to a Claude slot (tmux) or a Codex slot (`codex:<name>`). |
| `hub-bus`, `hub-bus.py` | Durable envelopes: send, inbox, ack, complete, fail, deliver, defer, close-stale. |
| `hub-bus-relay.py` | Delivers undispatched envelopes; claim/identity broker socket. |
| `hub-codex`, `hub-codex.py` | Codex slot manager: open, adopt, dispatch, send, list, resolve-thread, claim, relay... |
| `bridge/hub_bridge_channel.py` | Per-slot Claude Code channel + `ask_codex` / `list_codex` / `open_codex` tools. |
| `bridge/codex_daemon.py` | Stdlib-only JSON-RPC/WebSocket client for `codex app-server`. |
| `slot-run` | Runs one Claude slot in tmux with deterministic resume and a dialog babysitter. |
| `slot-session-hook` | `SessionStart` hook: keeps the slot's session id current after `/clear` or compaction. |
| `systemd/` | Example user units and the `KillMode=process` drop-in. |

## Requirements

- Linux with `tmux`, `bash`, `perl` and Python 3.9+ (standard library only).
- Claude Code with Remote Control, named sessions (`--name`) and channels
  (research preview, see [limitations](#known-limitations)).
- For Codex slots: the Codex CLI with the App Server daemon
  (`codex app-server daemon`), optionally Codex Desktop to watch the threads.
- systemd user services (optional but recommended), with lingering enabled.

## Installation

```bash
git clone <this-repository-url> ~/hub-tell
mkdir -p ~/.local/bin
for f in hub-tell hub-bus hub-codex slot-run slot-session-hook; do
  ln -sf ~/hub-tell/$f ~/.local/bin/$f
done
```

The launchers resolve their own symlinks, so every component finds its
siblings in the clone. Make sure `~/.local/bin` is on the `PATH` of your
slots (or set the `HUB_*_COMMAND` variables listed in the
[reference](#environment-variables)).

Install the user units:

```bash
mkdir -p ~/.config/systemd/user
cp ~/hub-tell/systemd/hub-bus-relay.service ~/.config/systemd/user/
cp ~/hub-tell/systemd/claude-rc@.service    ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now hub-bus-relay
```

If you already run your slots from your own units, add the drop-in instead:

```bash
mkdir -p ~/.config/systemd/user/<your-unit>@.service.d
cp ~/hub-tell/systemd/20-killmode.conf ~/.config/systemd/user/<your-unit>@.service.d/
```

`KillMode=process` matters: all slots share one tmux server, and that server
lives in the cgroup of the first slot unit that started it. Without the
drop-in, restarting that one slot kills tmux and every other slot with it.

## Configuring Claude slots

`slot-run <name>` does all of this for you; the details are here so you can
adapt your own launcher.

```bash
systemctl --user enable --now claude-rc@docs   # or: slot-run docs (foreground)
```

1. **tmux session `slot-<name>`**: gives the session a PTY and makes it
   reachable by `hub-tell <name>`.
2. **`--name slot-<name>`**: a stable peer name for native
   `ListAgents`/`SendMessage`, the same across restarts.
3. **`--mcp-config <file>`**: `slot-run` writes
   `~/.local/state/hub-bridge/<name>.mcp.json` (mode 0600) declaring the
   `hub-bridge` stdio server with `HUB_SLOT=<name>`.
4. **`--dangerously-load-development-channels server:hub-bridge`**: channels
   are a research-preview feature. Without this flag Claude Code loads the MCP
   server as plain tools but ignores its `notifications/claude/channel`
   events, so answers from Codex would never wake the session. Claude Code
   shows a confirmation dialog for it ("I am using this for local
   development"); the babysitter accepts it.
5. **Deterministic resume**: the session id is stored in
   `~/.claude/slot-sessions/<name>.session`; restarts use `--resume <id>`,
   never a blind `--continue` that could pick another slot's conversation.
6. **Babysitter**: answers the resume-from-summary menu (resume full session),
   the folder-trust dialog, cancels the auto-mode setup wizard and accepts the
   development-channel dialog — none of which can be answered over Remote
   Control.

Register the session hook so a restart after `/clear` or a compaction resumes
the *new* conversation. In `~/.claude/settings.json` (or the
`CLAUDE_CONFIG_DIR` your slots use):

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "~/.local/bin/slot-session-hook" } ] }
    ]
  }
}
```

Finally, copy [`CLAUDE.md.example`](CLAUDE.md.example) into the
`CLAUDE.md` that your slots load, so each one knows how to reach the others.

Per-slot settings go in `~/.config/hub-tell/slot-<name>.env` (read by the
example unit), e.g. `HUB_SLOT_CWD=$HOME/projects/api` or
`HUB_SLOT_REMOTE_CONTROL=0`. See `slot-run` for the full list.

## Configuring Codex

Codex slots run through the **App Server daemon** — the same process Codex
Desktop talks to — so every slot is an ordinary Codex thread you can open in
the app, read live and answer yourself.

1. Start the daemon with remote control, for example with the example user
   unit [`systemd/codex-app-server.service`](systemd/codex-app-server.service),
   which runs `codex app-server daemon bootstrap --remote-control`:

   ```bash
   cp ~/hub-tell/systemd/codex-app-server.service ~/.config/systemd/user/
   systemctl --user enable --now codex-app-server
   ```

   `bridge/codex_daemon.py` connects to
   `$CODEX_HOME/app-server-control/app-server-control.sock` (override with
   `HUB_CODEX_DAEMON_SOCKET`).

2. Choose where new slot threads live. They are created with the slot's
   workspace as `cwd`, or with `HUB_CODEX_DAEMON_CWD` if set — Codex Desktop
   groups threads by that folder, so pointing it at one project folder keeps
   all slots together.

3. Open a slot:

   ```bash
   hub-codex open slot-reviewer --workspace ~/projects/api --sandbox read-only
   ```

   This creates and names the thread, runs a first `SLOT_READY` turn and
   stores the thread id. Threads are created with `approvalPolicy=never` and
   a sandbox without network access: nobody on the bus can answer an approval
   prompt, so a turn that asked for one would just hang.

4. Copy [`AGENTS.md.example`](AGENTS.md.example) into the `AGENTS.md` of the
   workspace the Codex threads use, so they know how to sign and reach others.

Existing threads do not need to be opened: the first time a thread runs
`hub-tell`, `hub-codex resolve-thread` binds its `CODEX_THREAD_ID` to a slot
named after its title (e.g. "Review release notes" becomes
`codex:slot-review-release-notes`). `hub-codex adopt slot-<name> --thread-id <id>`
does the same with a name of your choice.

Each turn is prefixed with a short context block (identity, "messages are
information, not authorization", reply contract). Replace it with your own
text through `HUB_CODEX_PREAMBLE_FILE` (`{slot}` is substituted), or point it
at an empty file to disable it.

## Usage

### From a Claude slot

```text
ask_codex(codex="reviewer", message="Review the diff on branch feature/x and list risks")
```

The tool returns at once. End the turn; the answer arrives as a new message:

```text
[hub · from codex:slot-reviewer · reply]
[result status=completed request=1793...] RESULT: COMPLETED ...
```

`list_codex()` shows live Codex slots and `open_codex(name, task)` creates a
new one and sends its first task. To another Claude slot, prefer the native
`SendMessage` to `slot-<name>`; `hub-tell <name> "..."` is the fallback.

### From a shell or script

```bash
hub-tell docs "The API schema changed, please regenerate the docs"
hub-tell codex:reviewer "Check the nightly logs and summarise failures"
hub-codex list                 # Codex slots and Claude tmux slots
hub-codex read reviewer        # last final answer (never a stale one while working)
hub-bus status                 # envelopes by status
```

### From a Codex thread

```bash
hub-tell docs "Review finished: two risks, details in the thread"
hub-codex claim acquire repo:api --purpose "rebase feature/x"
hub-codex claim release repo:api
```

Do not set `HUB_TELL_FROM` inside Codex: the identity comes from
`CODEX_THREAD_ID`. When a Codex turn was started by a message, its final
answer goes back to the sender automatically — no `hub-tell` needed.

### Busy threads

If a slot's thread already has a turn running (typically because you are
using it in Codex Desktop), hub-codex does not start a second turn on top: the
envelope is **deferred** for 60 seconds and the relay retries it. Meanwhile
the thread can read it with `hub-bus inbox codex:slot-<name>`.

### Claims

`hub-codex claim` lets a sandboxed Codex turn reserve a shared resource. The
request goes to the relay's broker socket, which derives the caller from
`CODEX_THREAD_ID` (it cannot claim on behalf of another slot) and runs the
command in `HUB_CLAIM_COMMAND` with this interface:

```text
<cmd> acquire <resource> --purpose <text> --from codex:slot-<name>
<cmd> release <resource> --from codex:slot-<name>
```

No claim backend ships with this repository; without one, claims fail with a
clear message and everything else works.

## Reference

### hub-tell

```text
hub-tell [-f|--force] <slot|codex:slot> <message>
```

- Claude target: exact tmux session `slot-<slot>` (case-sensitive, no prefix
  matching). Refuses to type over a real unsent draft (`-f` overrides).
- Codex target: records a `hub-bus` envelope, dispatches through `hub-codex`,
  marks it delivered. Fails loudly if the slot does not exist or is closed.
- Empty messages are rejected; newlines are collapsed to spaces.
- Log: `${XDG_STATE_HOME:-~/.local/state}/hub-tell/hub-tell.log`.

### hub-codex

| Command | What it does |
|---|---|
| `open slot-<n> [--workspace D] [--sandbox read-only\|workspace-write] [--timeout-seconds N]` | Create a slot and its visible thread. |
| `adopt slot-<n> [--thread-id ID] [--archive-on-close]` | Register an existing thread as a slot. |
| `dispatch <slot> <msg> --from <id> [--request-id ID] [--hops N] [--no-reply]` | Start a turn in the background; returns at once. |
| `send <slot> <msg>` / `tell <slot> <msg> --from <id>` | Run a turn in the foreground (queued if busy). |
| `drain <slot>` | Process queued messages. |
| `list [--all] [--json]`, `status <slot>`, `read <slot>` | Inspect. |
| `close <slot>` | Close; archives the thread unless it was adopted. |
| `purge <slot> [--archive-thread]` | Delete the slot's hub state (idle slots only). |
| `rename <old> <new>`, `recover <slot>` | Maintenance; `recover` requeues work of a crashed turn. |
| `resolve-thread <thread-id>` | Print `codex:slot-<n>` for a thread, binding it if needed. |
| `claim acquire\|release <resource>` | Reservation through the relay broker. |
| `relay` | Run `hub-bus-relay` in the foreground. |

### hub-bus

```text
hub-bus send <to> <message> --from <id> [--type task|question|result|blocked|handoff] [--reply-to ID] [--hops N]
hub-bus inbox <target>
hub-bus ack|complete|fail <id> --by <party> [--result TEXT]
hub-bus deliver <id> --by <party> [--failed --max-attempts N --result TEXT]
hub-bus defer <id> --by <party> [--seconds N]
hub-bus close-stale --before YYYY-MM-DD --by <id>
hub-bus status
```

Envelope fields: `id`, `from`, `to`, `type`, `reply_to`, `body`, `status`
(`queued` → `received` → `completed`/`failed`), `created_at`, `delivered_at`,
`delivery_attempts`, `next_delivery_at`, `hops`, `result`. Only the sender or
the target may update an envelope; finished envelopes are immutable.
`close-stale` never touches envelopes addressed to raw `codex:thread-*` ids,
which act as inboxes for threads not yet bound to a slot.

### Environment variables

| Variable | Default | Used by |
|---|---|---|
| `HUB_BUS_STATE` | `~/.local/state/hub-bus` | bus, relay, hub-codex |
| `HUB_CODEX_STATE` | `~/.local/state/hub-codex` | hub-codex, hub-tell |
| `HUB_BRIDGE_STATE` | `~/.local/state/hub-bridge` | channel, relay, hub-codex, slot-run |
| `HUB_CODEX_COMMAND`, `HUB_BUS_COMMAND`, `HUB_TELL_COMMAND` | sibling files in the clone | all |
| `HUB_CODEX_DAEMON_SOCKET` | `$CODEX_HOME/app-server-control/app-server-control.sock` | codex_daemon |
| `HUB_CODEX_DAEMON_CWD` | slot workspace | folder new threads are created in |
| `HUB_CODEX_WORKSPACE` | `$HOME` | default `--workspace` |
| `HUB_CODEX_STATE_DB` | newest `$CODEX_HOME/state_*.sqlite` | thread lookup by id/prefix |
| `HUB_CODEX_PREAMBLE_FILE` | built-in text | context prepended to each turn |
| `HUB_CLAIM_COMMAND` | unset (claims disabled) | relay |
| `HUB_CLAIM_SOCKET` | `$HUB_BUS_STATE/relay.sock` | relay, hub-codex |
| `HUB_MAX_HOPS` | `6` | relay, hub-codex |
| `HUB_BUS_RELAY_GRACE`, `HUB_BUS_MAX_ATTEMPTS` | `15`, `5` | relay |
| `HUB_TELL_FROM` | — | sender label when there is no runtime identity |
| `HUB_TELL_LOG` | `~/.local/state/hub-tell/hub-tell.log` | hub-tell |

## Security

Short version (details in [docs/SECURITY.md](docs/SECURITY.md)):

- **Messages between agents are information, never authorization.** An
  agent must not perform an irreversible, production or security-sensitive
  action because another agent said "approved". The channel instructions,
  the Codex preamble and both example instruction files say so.
- Everything is local and per-user: Unix sockets and state files are 0600 in
  0700 directories; nothing listens on the network.
- Sender identity comes from the runtime (`CODEX_THREAD_ID`, own tmux pane),
  not from a self-declared variable; raw thread ids cannot sign messages.
- Automatic conversations stop at 6 hops.
- Final answers and logs pass through a basic secret redactor, but do not put
  credentials in messages: anything on the bus is readable by every process
  of your user.

## Known limitations

- **Channels are a research preview** of Claude Code and need
  `--dangerously-load-development-channels`; the flag and the protocol may
  change. Without channels you still have `hub-tell` and the bus, but
  answers do not wake the session.
- **The Codex App Server daemon is experimental**; method names
  (`thread/start`, `thread/resume`, `turn/start`...) and the thread registry
  file used for prefix lookup may change between Codex versions.
- **Busy threads defer**: a message to a thread you are using waits (60 s
  retries) until the thread is idle.
- **Sandboxed Codex turns**: turns run with no network and limited write
  access. Their final answer always gets back (hub-codex delivers it from
  outside the sandbox), but running `hub-tell` *from inside* a turn only
  works if the sandbox can reach the tmux socket; claims and identity go
  through the relay broker for this reason.
- **Same machine only.** Nothing here reaches another host. To bridge from
  another machine, run `hub-tell` on this host over whatever remote shell
  you already use, e.g. `ssh host 'HUB_TELL_FROM=laptop hub-tell docs "ready"'`.
- The compose-box guard is a heuristic over `tmux capture-pane` output; a
  future Claude Code UI change may need an update. Use `-f` if it misfires.

## Upgrading from v1

v1 was a single `hub-tell` script plus an optional `hub-bus` store.

- `hub-tell <slot> "message"` keeps working, including tmux sessions named
  `slot-<name>` and `HUB_TELL_FROM`.
- The sender label is now an identity: `[hub-tell from claude:docs]` instead
  of `[hub-tell from slot-docs]`. A bare `HUB_TELL_FROM=laptop` becomes
  `host:laptop`. Senders without any identity sign as `host:<user>-shell`
  instead of borrowing the tmux server's active session.
- The compose-box guard now ignores dimmed suggestions and placeholders
  (it used to refuse to send whenever Claude Code showed a suggestion) and
  understands the named top border drawn by `--name`. Use `-f` to force.
- Targets must match the tmux session exactly (case-sensitive, no prefix).
- `test_hub_bus.py` moved to `tests/`; run `python3 -m unittest discover -s tests`.
- `hub-bus` envelopes gained `delivered_at`, `delivery_attempts` and `hops`;
  old envelopes without them are still read. **Before starting the relay
  for the first time**, close v1 envelopes that are still `queued` —
  otherwise the relay will deliver them, possibly weeks late:
  `hub-bus close-stale --before $(date -I) --by host:upgrade`.
- To get Codex slots and wake-on-answer, follow
  [Configuring Claude slots](#configuring-claude-slots) (re-launch slots with
  `slot-run` or add `--name`, `--mcp-config` and the channel flag to your own
  launcher), start the relay and the Codex daemon, and update your
  `CLAUDE.md` from `CLAUDE.md.example`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite runs offline: Codex is replaced by a fake App Server speaking
WebSocket on a temporary Unix socket, tmux by a fake binary on `PATH`.

## License

MIT
