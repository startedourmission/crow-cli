"""Reverse agent: the agent runs in a sandboxed "server" home, but its
crow_client tool calls write only into the client workspace; the guard
denies server-side tools; the proxy survives a bridge restart for reads.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
CROW = str(REPO / "crow")
sys.path.insert(0, str(REPO))

from crowcli import reverse, tools  # noqa: E402


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.TemporaryDirectory(prefix="ccw-", dir="/tmp")

    def tearDown(self):
        self.ws.cleanup()

    def test_tools_are_sandboxed_to_client_root(self):
        info = tools.run_tool(self.ws.name, "workspace_info", {})
        self.assertEqual(info["root"], os.path.realpath(self.ws.name))
        tools.run_tool(self.ws.name, "write_file",
                       {"path": "a.txt", "text": "hello"})
        self.assertEqual(tools.run_tool(
            self.ws.name, "read_file", {"path": "a.txt"})["text"], "hello")
        with self.assertRaises(ValueError):
            tools.run_tool(self.ws.name, "read_file",
                           {"path": "../outside.txt"})
        with self.assertRaises(ValueError):
            tools.run_tool(self.ws.name, "write_file",
                           {"path": "a.txt", "text": "overwrite"})
        edited = tools.run_tool(self.ws.name, "edit_file",
                                {"path": "a.txt", "old_text": "hell",
                                 "new_text": "HELL"})
        self.assertTrue(edited["written"])

    def test_guard_only_allows_crow_client(self):
        import io
        for name, allowed in [("mcp__crow_client__shell", True),
                              ("Read", False), ("Bash", False),
                              ("TodoWrite", True)]:
            old = sys.stdin
            sys.stdin = io.StringIO(json.dumps({"tool_name": name}))
            try:
                decision = reverse.guard()["hookSpecificOutput"][
                    "permissionDecision"]
            finally:
                sys.stdin = old
            self.assertEqual(decision, "allow" if allowed else "deny", name)


class ReverseLocalTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="cch-", dir="/tmp")
        self.root = tempfile.TemporaryDirectory(prefix="ccr-", dir="/tmp")
        self.ws = tempfile.TemporaryDirectory(prefix="ccw-", dir="/tmp")
        self.env = dict(os.environ, CROWCLI_HOME=self.home.name,
                        CROWCLI_SESSION_ROOT=self.root.name,
                        SHELL="/bin/sh", TERM="xterm-256color")

    def tearDown(self):
        subprocess.run([CROW, "kill-session", "-t", "rag"], env=self.env,
                       capture_output=True, timeout=30)
        for bridge in Path(self.home.name).glob("bridges/*.json"):
            try:
                state = json.loads(bridge.read_text())
                os.kill(state["pid"], 15)
            except (ValueError, KeyError, OSError):
                pass
        self.home.cleanup()
        self.root.cleanup()
        self.ws.cleanup()

    def cli(self, *args, **kwargs):
        return subprocess.run([CROW] + list(args), env=self.env,
                              capture_output=True, text=True, timeout=60,
                              **kwargs)

    def test_fake_agent_writes_only_to_client_workspace(self):
        result = self.cli("reverse-agent", "--local", "--provider", "fake",
                          "--dir", self.ws.name, "-s", "rag", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        target = Path(self.ws.name) / "reverse-agent-was-here.txt"
        deadline = time.time() + 15
        while not target.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(target.exists(),
                        "agent tool call never reached the client")
        self.assertIn("client", target.read_text())
        # Nothing was written into the sandboxed server workspace.
        server_side = list(Path(self.home.name).rglob(
            "reverse-agent-was-here.txt"))
        self.assertEqual(server_side, [])
        # The agent's PTY session is persistent and captures its output.
        listing = self.cli("ls").stdout
        self.assertIn("rag", listing)
        self.assertIn("running", listing)
        capture = self.cli("capture", "-t", "rag").stdout
        self.assertIn("client workspace is", capture)

    def wait_capture(self, target, text):
        deadline = time.time() + 15
        while time.time() < deadline:
            if text in self.cli("capture", "-t", target).stdout:
                return True
            time.sleep(0.2)
        return False

    def test_split_and_new_window_start_another_agent(self):
        result = self.cli("reverse-agent", "--local", "--provider", "fake",
                          "--dir", self.ws.name, "-s", "rag", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        split = self.cli("split-window", "-t", "rag")
        self.assertEqual(split.returncode, 0, split.stderr)
        self.assertIn("(agent)", split.stdout)
        self.assertTrue(self.wait_capture("rag:0.1", "client workspace is"),
                        "split pane did not start the agent")
        new = self.cli("new-window", "-t", "rag")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout.strip(), "rag:fake-2")
        self.assertTrue(self.wait_capture("rag:1", "client workspace is"),
                        "new window did not start the agent")
        state = json.loads(
            (Path(self.home.name) / "state.json").read_text())
        session = state["sessions"][0]
        panes = [p for w in session["windows"] for p in w["panes"]]
        self.assertEqual(len(panes), 3)
        for pane in panes:
            self.assertEqual(pane["meta"]["provider"], "fake")
            self.assertEqual(pane["meta"]["client_root"],
                             os.path.realpath(self.ws.name))
        # Each agent got its own launch, not a copy of the first one's.
        self.assertEqual(len(set(p["command"] for p in panes)), 3)
        refused = self.cli("split-window", "-t", "rag", "--", "vi")
        self.assertNotEqual(refused.returncode, 0)

    def test_change_folder_restarts_agent_in_new_folder(self):
        from unittest import mock
        from crowcli import cli, registry
        result = self.cli("reverse-agent", "--local", "--provider", "fake",
                          "--dir", self.ws.name, "-s", "rag", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        other = tempfile.TemporaryDirectory(prefix="ccw2-", dir="/tmp")
        self.addCleanup(other.cleanup)
        with mock.patch.dict(os.environ, self.env):
            old = registry.active_pane(registry.load_registry().find("rag")
                                       ["windows"][0])

            def move(reg, root=other.name):
                win = reg.find("rag")["windows"][0]
                return cli.change_reverse_folder(reg, win, old["id"], root)
            pane = cli.with_registry(move)
            win = registry.load_registry().find("rag")["windows"][0]
        root = os.path.realpath(other.name)
        self.assertNotEqual(pane["id"], old["id"])
        self.assertEqual([p["id"] for p in win["panes"]], [pane["id"]])
        self.assertEqual(win["layout"], {"leaf": pane["id"]})
        self.assertEqual(win["panes"][0]["meta"]["client_root"], root)
        self.assertEqual(win["meta"]["client_root"], root)
        target = Path(root) / "reverse-agent-was-here.txt"
        deadline = time.time() + 15
        while not target.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(target.exists(), "new agent never reached folder")
        self.assertTrue(self.wait_capture("rag", "client workspace is "
                                          + root))
        # Same folder again is a no-op; a missing folder is refused.
        with mock.patch.dict(os.environ, self.env):
            self.assertIsNone(cli.with_registry(
                lambda reg: cli.change_reverse_folder(
                    reg, reg.find("rag")["windows"][0], pane["id"], root)))
            with self.assertRaises(ValueError):
                cli.with_registry(lambda reg: cli.change_reverse_folder(
                    reg, reg.find("rag")["windows"][0], pane["id"],
                    root + "/nope"))

    def test_prefix_split_while_attached_keeps_the_session(self):
        import pty
        import select
        import struct
        import termios
        import fcntl
        result = self.cli("reverse-agent", "--local", "--provider", "fake",
                          "--dir", self.ws.name, "-s", "rag", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 100, 0, 0))
        proc = subprocess.Popen([CROW, "attach", "-t", "rag"], stdin=slave,
                                stdout=slave, stderr=slave, env=self.env)
        os.close(slave)
        out = b""

        def drain(wait):
            nonlocal out
            end = time.time() + wait
            while time.time() < end:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        out += os.read(master, 65536)
                    except OSError:
                        break
        drain(2.0)
        for data, wait in [(b"\x02%", 4.0), (b"\x02c", 4.0),
                           (b"\x02d", 2.0)]:
            os.write(master, data)
            drain(wait)
        proc.wait(timeout=10)
        os.close(master)
        self.assertEqual(proc.returncode, 0, out[-600:])
        self.assertIn("리버스 에이전트(fake) 시작 중".encode(), out)
        self.assertIn(b"detached from rag", out)
        state = json.loads(
            (Path(self.home.name) / "state.json").read_text())
        windows = state["sessions"][0]["windows"]
        self.assertEqual([len(w["panes"]) for w in windows], [2, 1])
        self.assertIn("client workspace is",
                      self.cli("capture", "-t", "rag:0.1").stdout)


class ProxyReconnectTests(unittest.TestCase):
    """Reads replay across a bridge restart; the agent never reconnects."""

    def setUp(self):
        self.ws = tempfile.TemporaryDirectory(prefix="ccw-", dir="/tmp")
        Path(self.ws.name, "note.txt").write_text("from-client")
        self.token = "test-token"
        self.port = 18777
        self.bridge = None
        self.proxy = None
        self.start_bridge()
        config = json.dumps({"host": "127.0.0.1", "port": self.port,
                             "token": self.token})
        self.proxy = subprocess.Popen(
            [CROW, "agent-proxy", config], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1,
            env=dict(os.environ))
        self.counter = 0

    def start_bridge(self):
        self.bridge = subprocess.Popen(
            [CROW, "agent-bridge", "--root", self.ws.name,
             "--token", self.token, "--port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=dict(os.environ))
        self.bridge.stdout.readline()

    def tearDown(self):
        for proc in (self.proxy, self.bridge):
            if proc and proc.poll() is None:
                proc.terminate()
        self.ws.cleanup()

    def call(self, name, arguments=None):
        self.counter += 1
        self.proxy.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": self.counter, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}) + "\n")
        self.proxy.stdin.flush()
        return json.loads(self.proxy.stdout.readline())

    def test_read_replays_after_bridge_restart(self):
        first = self.call("read_file", {"path": "note.txt"})
        self.assertIn("from-client",
                      first["result"]["content"][0]["text"])
        self.bridge.terminate()
        self.bridge.wait(timeout=5)
        # Sent while the bridge is down: queued, then replayed (read-only).
        import threading
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(
                self.call("read_file", {"path": "note.txt"})))
        worker.start()
        time.sleep(0.5)
        self.start_bridge()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertIn("from-client",
                      result["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()


class RemotePathTests(unittest.TestCase):
    def test_remote_quote_keeps_tilde_expandable(self):
        from crowcli.cli import _remote_quote
        self.assertEqual(_remote_quote("~/.crow-cli/bin/crow-session"),
                         "$HOME/.crow-cli/bin/crow-session")
        self.assertEqual(_remote_quote("~"), "$HOME")
        self.assertEqual(_remote_quote("a b"), "'a b'")

    def test_find_provider_beyond_minimal_ssh_path(self):
        bindir = tempfile.mkdtemp(prefix="ccb-", dir="/tmp")
        tool = Path(bindir) / "claude"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        old_dirs, old_path = reverse.EXTRA_PATH_DIRS, os.environ.get("PATH")
        reverse.EXTRA_PATH_DIRS = [bindir]
        os.environ["PATH"] = "/usr/bin:/bin"
        try:
            self.assertEqual(reverse._find_provider("claude"), str(tool))
        finally:
            reverse.EXTRA_PATH_DIRS = old_dirs
            if old_path is not None:
                os.environ["PATH"] = old_path

    def test_claude_folder_trust_is_preaccepted(self):
        import json
        config = Path(tempfile.mkdtemp(prefix="cct-", dir="/tmp")) / "c.json"
        config.write_text(json.dumps({"projects": {"/other": {"x": 1}},
                                      "userID": "keep"}))
        self.assertTrue(reverse.trust_claude_workspace("/ws", str(config)))
        data = json.loads(config.read_text())
        self.assertTrue(data["projects"]["/ws"]["hasTrustDialogAccepted"])
        self.assertEqual(data["projects"]["/other"], {"x": 1})
        self.assertEqual(data["userID"], "keep")
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        config.write_text("{broken")
        self.assertFalse(reverse.trust_claude_workspace("/ws", str(config)))
        self.assertEqual(config.read_text(), "{broken")  # never clobbered

    def test_reverse_session_names_and_reuse(self):
        from crowcli import cli

        class Reg(object):
            def __init__(self, sessions):
                self.data = {"sessions": sessions}

            def find(self, name):
                return next((s for s in self.data["sessions"]
                             if s["name"] == name), None)
        root = cli.tools_mod.canonical("/tmp")
        reg = Reg([{"name": "reverse-claude", "kind": "reverse-agent",
                    "windows": [{"meta": {"provider": "claude",
                                          "client_root": root,
                                          "remote_host": "u@a"}}]}])
        self.assertEqual(cli.free_name(reg, "reverse-codex"), "reverse-codex")
        self.assertEqual(cli.free_name(reg, "reverse-claude"),
                         "reverse-claude-2")
        self.assertEqual(cli.find_reverse_session(
            reg, "claude", "/tmp", {"address": "u@a"}), "reverse-claude")
        self.assertIsNone(cli.find_reverse_session(  # other host
            reg, "claude", "/tmp", {"address": "u@b"}))
        self.assertIsNone(cli.find_reverse_session(  # other folder
            reg, "claude", "/usr", {"address": "u@a"}))

    def test_remote_exec_never_hands_ssh_the_terminal(self):
        from unittest import mock
        from crowcli import cli
        with mock.patch.object(cli.subprocess, "run") as run:
            cli.remote_exec({"address": "u@h"}, ["true"])
            self.assertIs(run.call_args.kwargs["stdin"],
                          cli.subprocess.DEVNULL)
            cli.remote_exec({"address": "u@h"}, ["cat"], stdin_data="x")
            self.assertEqual(run.call_args.kwargs["input"], "x")



class RemoteForwardTests(unittest.TestCase):
    """Remote reverse panes: one -R per bridge port, one -L per pane,
    kept across re-attaches and closed when their pane is gone."""

    def test_forwards_cover_every_pane_and_persist(self):
        from unittest import mock
        from crowcli import cli
        home = tempfile.mkdtemp(prefix="cch-", dir="/tmp")
        meta = {"client_root": "/ws", "provider": "claude",
                "remote_host": "u@h", "bridge_remote_port": 50000,
                "remote_socket": "/tmp/crow-cli-sessions-501/p1.sock"}

        def pane(pid):
            return {"id": pid, "name": pid, "cwd": "/", "command": None,
                    "meta": dict(meta)}
        win1 = {"id": "w1", "name": "claude", "meta": dict(meta),
                "panes": [pane("p1"), pane("p2")], "active_pane": 0,
                "layout": {"split": "v", "ratio": 0.5, "a": {"leaf": "p1"},
                           "b": {"leaf": "p2"}}}
        win2 = {"id": "w2", "name": "claude-2", "meta": dict(meta),
                "panes": [pane("p3")], "active_pane": 0,
                "layout": {"leaf": "p3"}}
        session = {"name": "r", "kind": "reverse-agent",
                   "windows": [win1, win2]}

        class Reg(object):
            data = {"sessions": [session]}
        procs = []

        def popen(args, **_kwargs):
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.args = args
            if "-L" in args:
                Path(args[args.index("-L") + 1].split(":")[0]).touch()
            procs.append(proc)
            return proc
        forwards = {}
        with mock.patch.dict(os.environ, {"CROWCLI_HOME": home}), \
                mock.patch.object(cli, "ensure_bridge",
                                  return_value={"port": 40000,
                                                "token": "t"}), \
                mock.patch.object(cli.subprocess, "Popen", popen):
            cli.ensure_reverse_forwards(Reg(), session, forwards)
            self.assertEqual(sorted(k[0] for k in forwards),
                             ["bridge", "pane", "pane", "pane"])
            remote = [p.args[p.args.index("-L") + 1].split(":")[1]
                      for p in procs if "-L" in p.args]
            self.assertEqual(sorted(remote), [
                "/tmp/crow-cli-sessions-501/%s.sock" % i
                for i in ("p1", "p2", "p3")])
            self.assertEqual(win2["meta"]["session_root"],
                             os.path.join(home, "forwards"))
            # Re-attach: nothing restarted.
            cli.ensure_reverse_forwards(Reg(), session, forwards)
            self.assertEqual(len(procs), 4)
            # p2 killed: its tunnel closes, the others stay.
            win1["panes"] = win1["panes"][:1]
            win1["layout"] = {"leaf": "p1"}
            gone = forwards[("pane", "p2")]
            cli.ensure_reverse_forwards(Reg(), session, forwards)
            gone.terminate.assert_called_once()
            self.assertNotIn(("pane", "p2"), forwards)
            self.assertEqual(len(procs), 4)
