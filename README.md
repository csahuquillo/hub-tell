# hub-tell

A small local message bus for coordinating [Claude Code](https://claude.com/claude-code)
and Codex sessions running on the same machine. Claude and Codex keep their own
contexts and permissions, while `hub-tell` lets them exchange tasks, checks and
results safely.

If you run several long-lived Claude Code sessions on one server — each one
its own project or automation, kept alive in `tmux` and reachable via
[Remote Control](https://docs.claude.com/en/docs/claude-code/remote-control)
so you can check in from your phone or laptop — you eventually want them to
be able to say "hey, I finished X, can you check Y" to each other. There's
no built-in way to do that between independent sessions on the same host.
`hub-tell` is the smallest thing that could plausibly work: since every
session already lives in its own `tmux` pane on the same box, one session
can just type into another session's pane.

## How it works

```
hub-tell <session-name> "<message>"
```

This resolves `<session-name>` to a tmux session called `slot-<session-name>`,
checks it exists and is safe to write to (see below), types the message into
its compose box, and confirms it with Enter. The receiving Claude Code
session sees it as a completely normal user message, prefixed with
`[hub-tell from <sender>]` so it knows where it came from.

No network call, no API, no extra credentials — it's `tmux send-keys` under
the hood.

## Install

1. Copy `hub-tell` to `~/bin/hub-tell` (or anywhere on your `PATH`) on the
   machine that hosts your sessions, and `chmod +x` it.
2. Name your tmux sessions `slot-<name>` for each Claude Code session you
   want reachable this way (this is the convention the script expects; feel
   free to fork and change the prefix).
3. Optionally, copy the snippet in [`CLAUDE.md.example`](CLAUDE.md.example)
   into that machine's `~/.claude/CLAUDE.md`, so every session that starts up
   there already knows `hub-tell` exists and how to use it - you don't have
   to explain it to each one by hand.

## Claude ↔ Codex

The host integration accepts both Claude destinations and Codex slots. Claude
can send work to Codex with the `codex:` prefix:

```bash
hub-tell codex:auditoria "Revisa los logs del despliegue y devuelve un resumen"
```

Codex can send a follow-up back to Claude by targeting its Claude slot:

```bash
HUB_TELL_FROM=codex:auditoria hub-tell operaciones "La revisión ha terminado: todo OK"
```

Messages to Codex are dispatched in the background and are serialized by the
slot lock/queue. A receiving agent can reply using `hub-tell` or the durable
`hub-bus` envelope described below. This is coordination between independent
agents, not a shared model context or shared credential store.

## Usage

### Durable correlated messages

For task assignment across Claude and Codex runtimes, the repository also
includes `hub-bus`. It persists a validated message envelope and supports
acknowledgements, correlated results, failures, and blocked handoffs:

```bash
./hub-bus send codex:worker "Check the deployment" --from claude:maestro
./hub-bus inbox codex:worker
./hub-bus ack <message-id> --by codex:worker
./hub-bus complete <message-id> --by codex:worker --result "OK"
./hub-bus status
```

The bus stores state under `${HUB_BUS_STATE:-~/.local/state/hub-bus}` with
private permissions, atomic writes, strict name validation, and a 32,000
character message limit. Do not put credentials or confidential data in
messages. Run `python3 -m unittest -v test_hub_bus.py` to execute its tests.

From inside any session (as a normal shell command it can run):

```bash
hub-tell docs "The API schema changed, please regenerate the docs"
```

The `docs` session receives it as a new turn and can act on it right away.

## Two things this had to get right

**Typing the message and hitting Enter must be two separate `tmux send-keys`
calls.** Sending `tmux send-keys -t session "text" Enter` in one call doesn't
reliably submit it — if the target is mid-render, the text can land in the
compose box while the Enter gets lost, leaving it stuck as an unsent draft.
Splitting it into `send-keys -l "$MSG"`, a short `sleep`, then a separate
`send-keys Enter` is what actually works reliably.

**Never write into a compose box that isn't empty.** This turned out to
matter a lot more than expected: in testing, real sessions frequently had an
unsent draft sitting in their compose box — usually because someone typed a
message from a phone or another Remote Control client and it hadn't been
submitted yet. Writing on top of that would concatenate both messages and
submit the mashed-up result, once even nearly overwriting a real,
already-composed reply to someone else. `hub-tell` captures the pane first,
compares the compose box against the placeholder hint Claude Code shows when
it's genuinely empty (e.g. `Try "fix lint errors"`), and refuses to send if
there's real text there instead.

## Limitations

- **Same machine only.** This only works between sessions that share a
  `tmux` server, i.e. sessions on the same host. It does **not** reach
  sessions running on a different machine, different operating systems, or
  other Claude Code clients in general.
- The compose-box detection is a text-pattern heuristic against
  `tmux capture-pane` output, not an API — it's been reliable in practice
  but could in principle be thrown off by an unusual terminal width or a
  future Claude Code UI change.
- `HUB_TELL_FROM=<name>` overrides the auto-detected sender. Set this when
  invoking `hub-tell` from outside a real tmux session (e.g. over SSH from
  a script) - `tmux display-message` isn't reliable there.

## Bridging across machines: what we found

The obvious next step is reaching a session on a *different* machine (your
laptop, say, talking to the server above). Three options, in the order we
actually evaluated them:

1. **Claude Code's native cross-session messaging** (`ListAgents` /
   `SendMessage`) — in principle this is exactly built for this. In
   practice, invoking `SendMessage` from an injected instruction gets
   blocked by Claude Code's own auto-mode safety classifier every time we
   tried, regardless of phrasing. That reads as a deliberate guardrail
   (stopping an injected instruction from making a session message other
   agents on its own), not a bug — so we stopped trying to work around it.
2. **Real remote access to the other machine** (SSH, an agent) — technically
   the most direct fix, but it means opening your personal laptop up to
   remote access just for this. That's a real change in attack surface, not
   "just another script," so we didn't do it by default.
3. **What we actually use**: when a human is present with a session on the
   *other* machine, that session already has access to both sides (its own
   local environment plus however it reaches the server) and can relay a
   message on request — see the `hub_tell_remote.py`-style pattern below.
   It's not 24/7 automatic delivery, but it needs no new infrastructure and
   doesn't touch anyone's attack surface.

If you want a fully automatic bridge with no session in the loop, the
realistic shape for that is an async mailbox (the server writes a pending
message somewhere, a session on the other machine checks it on startup) —
we haven't built this, since our own use case didn't need 24/7 delivery.

### Relay pattern (option 3)

Not included as a ready-made script here since it depends on how you reach
your server (SSH, a cloud provider's session-manager equivalent, etc.), but
the shape is: from a session on machine B, run whatever gets you a shell on
machine A, then invoke `hub-tell` there with `HUB_TELL_FROM` set to
something that identifies machine B, e.g.:

```bash
ssh host-a "HUB_TELL_FROM=laptop /home/you/bin/hub-tell backend 'ready for review'"
```

Contributions turning this into a proper option in the script are welcome.

## License

MIT
