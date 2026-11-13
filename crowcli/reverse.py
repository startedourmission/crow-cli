"""Reverse agent: agent CLI runs on the server, its tools run on the client.

Ported from Crow (agent-history.py reverse_prepare/reverse_proxy/
reverse_guard + ReverseSSH.swift): the server agent gets an MCP server
named `crow_client`, its own server-side tools are disabled and guarded,
and the proxy keeps one MCP session across bridge drops — reads replay,
side-effecting calls are never silently repeated.

Transport here is one explicit OpenSSH connection (`ssh -R` for the MCP
bridge, `ssh -L` for the PTY socket) instead of Crow's ephemeral client
sshd; the security shape is the same (server can only call back through
the forward the client opened, with a per-session token).
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

from . import tools

INSTRUCTIONS = (
    "This is a crow-cli reverse agent session. You run on a server using "
    "its saved login, but the ONLY work target is the client workspace "
    "{root}. Use crow_client MCP tools for ALL filesystem reads, edits, "
    "searches, git, tests, and shell commands. Server-side tools are "
    "disabled or denied. Never use server filesystem paths for project "
    "work. Begin by calling workspace_info. Use shell for searches or "
    "commands not covered by the other client tools. Web search and web "
    "fetch are fine for reading the web. Subagents you start must also "
    "use only crow_client tools on the client workspace {root}: say so in "
    "their prompt. Read AGENTS.md and CLAUDE.md on the client if present "
    "and not already included below.")

# Client project instruction files carried into the agent's instructions
# (the agent itself starts in a private server folder, so it would never
# pick them up on its own).
NOTE_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md",
              "AGENTS.md")
NOTES_MAX = 30000  # characters, all files together

# Server-side tools that touch no server files: web reads and subagents
# (which are held to the same guard).
OPEN_TOOLS = ("WebSearch", "WebFetch", "Task", "Agent", "TodoWrite",
              "AskUserQuestion", "update_plan", "request_user_input")

REPLAYABLE = {"workspace_info", "list_directory", "read_file"}

# ssh non-interactive shells get a minimal PATH; agent CLIs usually live
# in one of these (homebrew, ~/.local) on the server.
EXTRA_PATH_DIRS = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin",
                   "/usr/local/bin"]


def _find_provider(provider):
    path = os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_PATH_DIRS)
    return shutil.which(provider, path=path)
PROVIDERS = ("claude", "codex", "grok", "fake")


# ---------------------------------------------------------------- bridge
class Bridge(object):
    """Client side: TCP listener that answers crow_client tool calls."""

    def __init__(self, root, token, host="127.0.0.1", port=0):
        self.root = tools.canonical(root)
        self.token = token
        self.host = host
        self.port = port
        self.sock = None
        self._stop = False

    def serve_forever(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        # Announce the bound port for the parent process / tests.
        sys.stdout.write(json.dumps({"port": self.port}) + "\n")
        sys.stdout.flush()
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.error:
                break
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn):
        stream = conn.makefile("rw", encoding="utf-8", newline="\n")
        try:
            hello = stream.readline()
            if not hello or json.loads(hello).get("crow_bridge_token") \
                    != self.token:
                return
            stream.write(json.dumps({"crow_bridge": "ok"}) + "\n")
            stream.flush()
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    continue
                response = tools.handle_message(self.root, request)
                if response is not None:
                    stream.write(json.dumps(response,
                                            ensure_ascii=False) + "\n")
                    stream.flush()
        except (ValueError, socket.error, OSError):
            pass
        finally:
            try:
                conn.close()
            except socket.error:
                pass


# ----------------------------------------------------------------- proxy
class Proxy(object):
    """Server side: stdio (agent MCP) <-> TCP (bridge), with reconnect.

    Crow's reverse_proxy semantics: one MCP session is presented to the
    agent across bridge drops. Replayable (read-only) calls are resent
    after reconnect; a side-effecting call that was already sent when the
    bridge dropped is failed as interrupted, never repeated.
    """

    def __init__(self, host, port, token):
        self.host, self.port, self.token = host, port, token
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.pending = {}
        self.conn = None
        self.stream = None
        self.down_since = None
        self.closed = False

    def reply(self, text):
        with self.out_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def fail(self, identifier, item, message):
        if item["tool"]:
            payload = {"jsonrpc": "2.0", "id": identifier,
                       "result": {"isError": True, "content": [
                           {"type": "text", "text": message}]}}
        else:
            payload = {"jsonrpc": "2.0", "id": identifier,
                       "error": {"code": -32000, "message": message}}
        self.reply(json.dumps(payload))

    def _send_locked(self, item):
        try:
            self.stream.write(item["line"] + "\n")
            self.stream.flush()
            item["sent"] = True
            return True
        except (socket.error, OSError, ValueError):
            return False

    def supervise(self):
        delay = 1
        while not self.closed:
            conn = None
            try:
                conn = socket.create_connection((self.host, self.port),
                                                timeout=5)
                stream = conn.makefile("rw", encoding="utf-8", newline="\n")
                stream.write(json.dumps(
                    {"crow_bridge_token": self.token}) + "\n")
                stream.flush()
                if json.loads(stream.readline()).get("crow_bridge") != "ok":
                    raise ValueError("bridge refused the proxy token")
            except (socket.error, OSError, ValueError):
                if conn is not None:
                    conn.close()
                time.sleep(delay)
                delay = min(delay * 2, 10)
                self._expire()
                continue
            delay = 1
            with self.lock:
                self.conn, self.stream = conn, stream
                self.down_since = None
                for item in list(self.pending.values()):
                    if not item["sent"]:
                        self._send_locked(item)
            try:
                for line in stream:
                    try:
                        identifier = json.loads(line).get("id")
                    except ValueError:
                        continue
                    with self.lock:
                        self.pending.pop(identifier, None)
                    self.reply(line.rstrip("\n"))
            except (socket.error, OSError):
                pass
            with self.lock:
                self.conn = self.stream = None
                if self.down_since is None:
                    self.down_since = time.monotonic()
                for identifier, item in list(self.pending.items()):
                    if not item["sent"]:
                        continue
                    if item["replay"]:
                        item["sent"] = False
                    else:
                        del self.pending[identifier]
                        self.fail(identifier, item,
                                  "Connection to the crow-cli client "
                                  "dropped while this call was running, so "
                                  "it may or may not have completed on the "
                                  "client. Check before retrying.")
                self._expire_locked()
            conn.close()

    def _expire_locked(self):
        if self.down_since is None or \
                time.monotonic() - self.down_since < 120:
            return
        for identifier, item in list(self.pending.items()):
            del self.pending[identifier]
            self.fail(identifier, item,
                      "The crow-cli client is disconnected. It reconnects "
                      "automatically when the client attaches again.")

    def _expire(self):
        with self.lock:
            self._expire_locked()

    def run(self):
        threading.Thread(target=self.supervise, daemon=True).start()
        for line in sys.stdin:
            line = line.rstrip("\n")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            identifier = message.get("id")
            with self.lock:
                if identifier is None:
                    if self.stream is not None:
                        self._send_locked({"line": line, "sent": False})
                    continue
                is_tool = message.get("method") == "tools/call"
                name = (message.get("params") or {}).get("name") \
                    if is_tool else None
                item = {"line": line,
                        "replay": not is_tool or name in REPLAYABLE,
                        "sent": False, "tool": is_tool}
                self.pending[identifier] = item
                if self.stream is not None:
                    self._send_locked(item)
                else:
                    self._expire_locked()
        self.closed = True
        return 0


# ----------------------------------------------------------------- guard
def guard():
    """PreToolUse hook: only crow_client tools may run (Crow's guard)."""
    request = json.load(sys.stdin)
    name = request.get("tool_name", "")
    allowed = re.search(r"(?:^|__)crow_client__", name) is not None
    allowed = allowed or name in OPEN_TOOLS
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason":
            "Use crow_client tools: this session works on the client, "
            "not on the agent server."}}


# --------------------------------------------------------------- prepare
def server_workspace(client_id, client_root, home=None):
    identity = client_id + "\n" + client_root
    digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
    base = os.path.join(home or os.path.expanduser("~"),
                        ".crow-cli", "reverse-agents")
    return os.path.join(base, digest)


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v)
                              for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(_toml, value)) + "]"
    return json.dumps(value)


FAKE_AGENT = '''\
import json, subprocess, sys
config = json.load(open(sys.argv[1]))
proc = subprocess.Popen(config["mcp_command"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, text=True, bufsize=1)
counter = [0]
def call(method, params=None):
    counter[0] += 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": counter[0],
        "method": method, "params": params or {}}) + "\\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())
call("initialize")
info = call("tools/call", {"name": "workspace_info", "arguments": {}})
root = json.loads(info["result"]["content"][0]["text"])["root"]
call("tools/call", {"name": "write_file", "arguments": {
    "path": "reverse-agent-was-here.txt",
    "text": "written by the server agent, stored on the client: " + root + "\\n"}})
print("crow-cli fake reverse agent: client workspace is " + root)
print("server cwd is " + __import__("os").getcwd() +
      " (the file above must NOT be here)")
sys.stdout.flush()
'''


def trust_claude_workspace(workspace, config_path=None):
    """Pre-accept Claude's folder-trust prompt for crow's private agent
    workspace (codex gets trust_level, grok gets --trust). Its default
    answer is "No, exit", which would end the agent on the first Enter."""
    path = config_path or os.path.join(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~"),
        ".claude.json")
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(workspace, {})
    if entry.get("hasTrustDialogAccepted") is True:
        return True
    entry["hasTrustDialogAccepted"] = True
    tmp = "%s.crow-%s" % (path, _rand())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def prepare(request):
    """Runs on the server. Writes the private launch dir and returns the
    command that starts the agent inside the caller's PTY session."""
    provider = request["provider"]
    if provider not in PROVIDERS:
        raise ValueError("Unsupported agent provider.")
    workspace = server_workspace(request["client_id"],
                                 request["client_root"],
                                 request.get("server_home"))
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    if os.path.islink(workspace) or os.stat(workspace).st_mode & 0o077:
        raise ValueError("Reverse agent storage must be private.")
    if request.get("runtime_path"):
        request["runtime_path"] = request["runtime_path"].replace(
            "$HOME", os.path.expanduser("~"))
    proxy_arg = json.dumps({"host": request.get("bridge_host", "127.0.0.1"),
                            "port": request["bridge_port"],
                            "token": request["token"]})
    proxy_cmd = [request["python"], "-m", "crowcli.cli",
                 "agent-proxy", proxy_arg]
    if request.get("runtime_path"):
        proxy_cmd = [request["python"], request["runtime_path"],
                     "agent-proxy", proxy_arg]
    server = {"command": proxy_cmd[0], "args": proxy_cmd[1:]}
    launch = os.path.join(workspace, "launch-%s" % _rand())
    os.makedirs(launch, mode=0o700)

    def write(relative, text, mode=0o600):
        target = os.path.join(launch, relative)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "x", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(target, mode)
        return target

    instructions = INSTRUCTIONS.format(root=request["client_root"])
    notes = request.get("project_notes")
    if notes:
        instructions += ("\n\nProject instructions from the client "
                         "workspace (already read for you; follow them):"
                         "\n\n" + notes[:NOTES_MAX + 2000])
    runtime_flag = []
    if request.get("runtime_path"):
        runtime_flag = [request["runtime_path"]]
    guard_cmd = " ".join(shlex.quote(p) for p in
                         [request["python"]] + runtime_flag +
                         (["-m", "crowcli.cli"] if not runtime_flag else [])
                         + ["agent-guard"])
    if request.get("runtime_path"):
        guard_cmd = "%s %s agent-guard" % (shlex.quote(request["python"]),
                                           shlex.quote(
                                               request["runtime_path"]))
    else:
        guard_cmd = "%s -m crowcli.cli agent-guard" % shlex.quote(
            request["python"])
    hooks = {"PreToolUse": [{"matcher": ".*", "hooks": [
        {"type": "command", "command": guard_cmd, "timeout": 5}]}]}

    if provider == "fake":
        config = write("fake.json", json.dumps({"mcp_command": proxy_cmd}))
        script = write("fake_agent.py", FAKE_AGENT)
        argv = [request["python"], script, config]
        write("launch.json", json.dumps({"cwd": workspace, "argv": argv}))
        command = " ".join(shlex.quote(a) for a in argv) + \
            "; exec ${SHELL:-/bin/sh} -l"
        return {"directory": workspace, "launch": launch,
                "command": command}

    executable = _find_provider(provider)
    if not executable:
        raise ValueError(provider.title() + " CLI is not installed on "
                         "the selected server.")
    help_text = subprocess.run([executable, "--help"], capture_output=True,
                               text=True, timeout=15).stdout
    if provider == "claude":
        required = ["--tools", "--mcp-config", "--strict-mcp-config",
                    "--settings"]
        if any(flag not in help_text for flag in required):
            raise ValueError("Update Claude on the server: this version "
                             "does not support client tool routing.")
        trust_claude_workspace(workspace)
        config = write("mcp.json", json.dumps(
            {"mcpServers": {"crow_client": server}}))
        settings = write("settings.json", json.dumps(
            {"hooks": hooks, "disableAllHooks": False}))
        argv = [executable, "--dangerously-skip-permissions", "--tools",
                ",".join(t for t in OPEN_TOOLS if t[0].isupper()),
                "--strict-mcp-config", "--mcp-config", config,
                "--settings", settings, "--append-system-prompt",
                instructions]
    elif provider == "codex":
        if "--dangerously-bypass-hook-trust" not in help_text:
            raise ValueError("Update Codex on the server: this version "
                             "does not support client tool routing.")
        argv = [executable, "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "-c", "mcp_servers.crow_client=" + _toml(server),
                "-c", "hooks.PreToolUse=" + _toml(hooks["PreToolUse"]),
                "-c", "developer_instructions=" + _toml(instructions),
                "-c", "projects." + json.dumps(workspace) +
                ".trust_level=\"trusted\"",
                "--enable", "hooks", "--disable", "shell_tool"]
        if "--search" in help_text:
            argv.append("--search")  # live web search
    else:  # grok, current TUI shape (Crow's fallback)
        if "--rules" not in help_text or "--always-approve" not in help_text:
            raise ValueError("Update Grok on the server: this version "
                             "does not support client tool routing.")
        denies = ["Bash", "Read", "Edit", "Write", "Grep", "Glob"]

        def plant(relative, text):
            target = os.path.join(workspace, relative)
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)

        plant(".grok/plugins/crow-client/.claude-plugin/plugin.json",
              json.dumps({"name": "crow-client", "version": "1.0.0"}))
        plant(".grok/plugins/crow-client/hooks/hooks.json",
              json.dumps({"hooks": hooks}))
        plant(".grok/config.toml", "\n".join([
            "[mcp_servers.crow_client]",
            "command = " + json.dumps(server["command"]),
            "args = " + json.dumps(server["args"]), "enabled = true", "",
            "[plugins]", 'enabled = ["crow-client"]', "",
            "[permission]", "deny = " + json.dumps(denies), ""]))
        argv = [executable, "--always-approve", "--no-leader",
                "--trust", "--rules", instructions]
        for rule in denies:
            argv += ["--deny", rule]

    if request.get("session_id"):
        session_id = request["session_id"]
        if not re.match(r"^[A-Za-z0-9_.\-]+$", session_id):
            raise ValueError("Invalid saved session ID.")
        if provider == "codex":
            argv += ["fork" if request.get("fork") else "resume", session_id]
        elif provider in ("claude", "grok"):
            argv += ["--resume", session_id] + \
                (["--fork-session"] if request.get("fork") else [])
    write("launch.json", json.dumps({"cwd": workspace, "argv": argv}))
    # The PTY daemon's PATH is the minimal ssh one; make sure the agent
    # (and anything it spawns, e.g. node) resolves like an interactive
    # login would.
    path_prefix = "PATH=%s:$PATH " % shlex.quote(os.pathsep.join(
        [os.path.dirname(executable)] + EXTRA_PATH_DIRS))
    command = path_prefix + "exec " + \
        " ".join(shlex.quote(a) for a in argv)
    return {"directory": workspace, "launch": launch, "command": command}


def project_notes(root):
    """The client folder's agent instruction files, joined and capped
    (runs on the client; prepare appends them to the instructions)."""
    parts, used = [], 0
    for relative in NOTE_FILES:
        path = os.path.join(root, relative)
        if not os.path.isfile(path) or used >= NOTES_MAX:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                text = stream.read(NOTES_MAX + 1)
        except OSError:
            continue
        room = NOTES_MAX - used
        clipped = text[:room]
        used += len(clipped)
        parts.append("--- %s ---\n%s%s" % (
            relative, clipped.rstrip("\n"),
            "\n[… truncated]" if len(text) > room else ""))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- history
HISTORY_LIMIT = 20


def _first_text(content):
    if isinstance(content, str):
        return content
    for item in content or []:
        if isinstance(item, dict) and item.get("type") in (
                "text", "input_text") and item.get("text"):
            return item["text"]
    return ""


def _title(text):
    text = " ".join((text or "").split())
    if not text or text.startswith("<"):  # command / context wrappers
        return ""
    return text[:80]


def _claude_history(workspace):
    base = os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.expanduser("~/.claude")
    folder = os.path.join(base, "projects",
                          re.sub(r"[^A-Za-z0-9]", "-", workspace))
    found = []
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".jsonl")]
    except OSError:
        return found
    for name in names:
        path = os.path.join(folder, name)
        title, summary = "", ""
        try:
            with open(path, encoding="utf-8", errors="replace") as stream:
                for count, line in enumerate(stream):
                    if count > 400 or (title and summary):
                        break
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") == "summary" and not summary:
                        summary = _title(entry.get("summary"))
                    elif entry.get("type") == "user" and not title and \
                            not entry.get("isMeta"):
                        title = _title(_first_text(
                            (entry.get("message") or {}).get("content")))
            updated = os.path.getmtime(path)
        except OSError:
            continue
        if summary or title:
            found.append({"id": name[:-len(".jsonl")],
                          "title": summary or title, "updated": updated})
    return found


def _codex_history(workspace):
    base = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    found = []
    for folder, _dirs, files in os.walk(os.path.join(base, "sessions")):
        for name in files:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(folder, name)
            ident, title = None, ""
            try:
                with open(path, encoding="utf-8", errors="replace") as stream:
                    for count, line in enumerate(stream):
                        if count > 400 or title:
                            break
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        payload = entry.get("payload") or {}
                        if entry.get("type") == "session_meta":
                            if payload.get("cwd") != workspace:
                                break
                            ident = payload.get("id")
                        elif ident and payload.get("type") == "user_message":
                            title = _title(payload.get("message"))
                        elif ident and payload.get("role") == "user":
                            title = _title(_first_text(
                                payload.get("content")))
                updated = os.path.getmtime(path)
            except OSError:
                continue
            if ident and title:
                found.append({"id": ident, "title": title,
                              "updated": updated})
    return found


def history(request):
    """Runs on the server: this client folder's earlier conversations
    with the provider, newest first, for resuming."""
    workspace = server_workspace(request["client_id"],
                                 request["client_root"],
                                 request.get("server_home"))
    provider = request["provider"]
    if provider == "claude":
        found = _claude_history(workspace)
    elif provider == "codex":
        found = _codex_history(workspace)
    else:
        found = []
    found.sort(key=lambda item: -item["updated"])
    return found[:HISTORY_LIMIT]


def _rand():
    return hashlib.sha256(os.urandom(16)).hexdigest()[:12]
