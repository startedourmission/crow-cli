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

