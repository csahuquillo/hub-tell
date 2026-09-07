# hub-tell

A tiny script that lets multiple [Claude Code](https://claude.com/claude-code)
sessions running on the same machine send each other messages, safely.

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

## Usage

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

## Limitations / roadmap

- **Same machine only.** This only works between sessions that share a
  `tmux` server, i.e. sessions on the same host. It does **not** reach
  sessions running on a different machine (say, your laptop vs. a cloud
  host), different operating systems, or other Claude Code clients in
  general.
- **Bridging across machines is the planned next step.** Claude Code does
  have a native cross-session messaging mechanism (`ListAgents` /
  `SendMessage`) that in principle could cover this, but getting it to fire
  reliably from an injected instruction needs more work - it's blocked by
  Claude Code's own auto-mode safety classifier when invoked that way. Until
  that's sorted out, `hub-tell` stays local-only. Contributions or ideas on
  this are welcome.
- The compose-box detection is a text-pattern heuristic against
  `tmux capture-pane` output, not an API — it's been reliable in practice
  but could in principle be thrown off by an unusual terminal width or a
  future Claude Code UI change.

## License

MIT
