# crow-cli — design

Rebuild of **only two** Crow (`../crow`) features as a tmux/herdr-like
terminal program. Everything else in Crow (editor, files, Git, browser,
Crowmap, Spaces UI) is out of scope.

## 1. What was taken from Crow

### A. Removed session persistence → `src/crow-session.c`

In Crow's current working tree these are deleted (uncommitted):

- `App/Services/SessionRuntime/crow-session.c`
- `Packages/CrowCore/Sources/CrowCore/PersistentTerminal.swift`
- `Tools/TerminalRuntime/test_runtime.py`, `scripts/bundle-terminal-runtime.py`
- and `docs/persistent-sessions-experiment.md` rewrites the PTY runtime away
  ("Crow does not ship its own background PTY … Long-running work stays in tmux").

Recovered from `HEAD` (`48cee8c Add Spaces, session runtime, …`). Behaviour
preserved, branding/env renamed (`CROWCLI_SESSION_ROOT`,
`/tmp/crow-cli-sessions-$uid`, daemon version 2):

- One **detached daemon per terminal id** (UUID), `forkpty`, real shell/agent.
- Private Unix socket per id (`0700` runtime dir, `0600` socket, owner checks).
- Attachments forward input / output / window size. **Disconnecting an
  attachment or quitting the client leaves the daemon alive.**
- Reattaching the same id reuses the process and replays bounded output
  (**4 MiB**) instead of re-running the startup command.
- Explicit stop signals foreground job + shell. A finished session is never
  silently restarted by an attach. Processes do **not** survive a reboot.
- Slow clients have bounded queues (8 MiB) and cannot block background jobs.

Frame protocol (1-byte kind + uint32-BE length), unchanged:

| dir | kind | meaning |
| --- | --- | --- |
| c→d | `A` | attach (daemon replies `ESC c` + replay) |
| c→d | `D` | terminal input |
| c→d | `W` | winsize: uint16 col, uint16 row |
| c→d | `K` | kill session |
| c→d | `I` | info request |
| d→c | `O` | output |
| d→c | `I` | current directory of the child |
| d→c | `X` | exit, uint32 exit code |

### B. Reverse agent → `crowcli/reverse.py`, `crowcli/tools.py`

Crow (`App/Services/ReverseSSH.swift`, `App/Agents/agent-history.py`
`reverse_prepare`/`reverse_serve`/`reverse_proxy`/`reverse_guard`,
`Packages/CrowCore/.../AgentTerminal.swift`):

- The agent CLI (Claude/Codex/Grok) **runs on the server** using the server's
  saved login; the **only work target is the client workspace**.
- All file/shell work goes through an MCP server named `crow_client`
  (`workspace_info`, `list_directory`, `read_file`, `write_file`, `edit_file`,
  `shell`), sandboxed to the client root (path-escape rejected).
- Server-side tools are disabled (`--tools ""` etc.) **and** a `PreToolUse`
  guard hook denies anything that is not `crow_client`.
- The MCP proxy on the server **keeps one MCP session across connection
  drops**: reads are replayed after reconnect; a side-effecting call that was
  already sent is reported interrupted, never silently repeated.
- System prompt states: you run on a server, the only target is the client
  workspace, begin with `workspace_info`.

crow-cli keeps that design and replaces Crow's ephemeral client `sshd` +
`ssh -R` (needed because Crow's client is a GUI app already holding an SSH
connection) with one explicit OpenSSH connection owned by the CLI:

```
client (your machine)                                server
┌────────────────────────────┐   ssh -R (MCP)      ┌──────────────────────┐
│ crow agent-bridge          │◀────────────────────│ crow agent-proxy     │
│  tools on CLIENT workspace │   ssh -L (PTY sock) │  (stdio ⇄ TCP,       │
│ crow (attach, prefix keys) │────────────────────▶│   reconnect/replay)  │
└────────────────────────────┘                     │ claude/codex/grok    │
                                                   │  └─ MCP crow_client ─┘
                                                   │ crow-session daemon  │
                                                   │  (PTY survives SSH)  │
                                                   └──────────────────────┘
```

- Agent PTY runs under the **same `crow-session` daemon on the server**, so
  the agent survives SSH/client disconnect (Crow: "Reverse-agent PTYs use
  the same service … processes survive a UI detach; access to client files
  still requires the reverse bridge to be connected").
- Remote install mirrors Crow's `PersistentTerminal.install`: sources are
  copied to `~/.crow-cli/bin` on the server and compiled there with `cc`
  (no root, no tmux, no Homebrew).

## 2. Terminal model (tmux/herdr mapping)

| tmux | herdr | crow-cli |
| --- | --- | --- |
| server | agent daemon | per-window `crow-session` daemons |
| session | workspace | session (registry `~/.crow-cli/state.json`) |
| window | tab | window (split layout, shown together) |
| pane | pane | pane (one PTY daemon each) |
| attach/detach | attach/detach | `crow attach`, prefix `C-b d` |
| `C-b c/n/p/0-9` | — | same prefix keys |
| `capture-pane` | — | `crow capture` (daemon replay) |
| `new -d` | — | `crow new -d` |

The attach client draws a status bar (session, windows, kind/provider,
clock, prefix hints) on the terminal's last row without a VT emulator: the
daemon is told a PTY height one row shorter than the real terminal, and
the bar is painted with save/restore-cursor after output batches (skipped
mid-escape-sequence) and once a second.

Deliberate v1 limits: windows are shown one at a time (switching re-attaches
and the daemon's replay redraws the screen); no simultaneous split rendering
— that needs a full terminal emulator, which tmux/herdr embed and Crow
never had. Session/window metadata persists in
`~/.crow-cli/state.json`; PTYs persist in their daemons.

## 3. Layout

```
src/crow-session.c     PTY daemon + attach client (C11, cc, no deps)
crowcli/registry.py    sessions/windows state (file-locked JSON)
crowcli/daemon.py      binary build/locate, socket frame client, spawn/stop/info/capture
crowcli/vt.py          per-pane VT screen (grid/SGR/alt-screen/scroll)
crowcli/client.py      attach renderer: composites panes, borders, bar
crowcli/tools.py       crow_client MCP tools, sandboxed to client workspace
crowcli/reverse.py     bridge (client), proxy (server), prepare/provider launch, guard
crowcli/cli.py         argparse CLI
crow                   executable entry (python3 stdlib only)
tests/                 runtime (ported from Crow), CLI e2e, reverse-agent e2e
```
