"""SSH keys / host check / ssh connect (Crow's SSH Keys + SSH command)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CROW = str(REPO / "crow")


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="cch-", dir="/tmp")
        self.user_home = tempfile.TemporaryDirectory(prefix="ccu-",
                                                     dir="/tmp")
        self.root = tempfile.TemporaryDirectory(prefix="ccr-", dir="/tmp")
        self.env = dict(os.environ, CROWCLI_HOME=self.home.name,
                        CROWCLI_SESSION_ROOT=self.root.name,
                        HOME=self.user_home.name,
                        SHELL="/bin/sh", TERM="xterm-256color")

    def tearDown(self):
        subprocess.run([CROW, "kill-session", "-t", "ssh-bad"],
                       env=self.env, capture_output=True, timeout=30)
        self.home.cleanup()
        self.user_home.cleanup()
        self.root.cleanup()

    def cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        result = subprocess.run([CROW] + list(args), env=self.env,
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            raise AssertionError("%s failed: %s%s" % (
                args, result.stdout, result.stderr))
        return result

    def test_key_lifecycle_and_host_binding(self):
        self.cli("key", "add", "work", "--passphrase", "")
        listing = self.cli("keys").stdout
        self.assertIn("work", listing)
        self.assertIn("SHA256:", listing)
        self.assertIn("ED25519", listing)
        shown = self.cli("key", "show", "work").stdout
        self.assertIn("ssh-ed25519", shown)
        # Bind the key to a host by name, then deletion must be refused.
        self.cli("host", "add", "bad", "--address", "root@127.0.0.1", "--key", "work")
        self.assertIn("crow_work", self.cli("hosts").stdout)
        refused = self.cli("key", "rm", "work", "--force", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("사용 중인 키", refused.stdout + refused.stderr)
        self.cli("host", "rm", "bad")
        self.cli("key", "rm", "work", "--force")
        self.assertNotIn("crow_work", self.cli("keys").stdout)
        self.assertFalse(Path(self.user_home.name, ".ssh",
                              "crow_work").exists())

    def test_key_import(self):
        self.cli("key", "add", "orig", "--passphrase", "")
        src = Path(self.user_home.name, ".ssh", "crow_orig")
        self.cli("key", "add", "copy", "--file", str(src))
        self.assertIn("copy", self.cli("keys").stdout)
        self.cli("key", "rm", "copy", "--force")
        self.assertTrue(src.exists())  # import unregisters only

    def test_key_rename_and_host_usage(self):
        self.cli("key", "add", "work", "--passphrase", "")
        self.cli("key", "rename", "work", "home")
        self.cli("host", "add", "box", "--address", "root@127.0.0.1",
                 "--key", "home")
        listing = self.cli("keys").stdout
        self.assertIn("home:", listing)
        self.assertIn("호스트: box", listing)
        clash = self.cli("key", "rename", "nope", "x", check=False)
        self.assertNotEqual(clash.returncode, 0)

    def test_host_check_reports_refused(self):
        self.cli("host", "add", "bad", "--address", "root@127.0.0.1", "--port", "1")
        result = self.cli("host", "check", "bad", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("연결 실패", result.stdout)
        self.assertIn("연결 거부", result.stdout)

    def test_ssh_creates_ssh_session(self):
        self.cli("host", "add", "bad", "--address", "root@127.0.0.1", "--port", "1")
        self.cli("ssh", "bad", "-d")
        listing = self.cli("ls").stdout
        self.assertIn("ssh-bad", listing)
        self.assertIn("[ssh]", listing)


class HostMenuTests(unittest.TestCase):
    def test_host_menu_actions(self):
        import sys
        sys.path.insert(0, str(REPO))
        from crowcli.client import Client
        host = {"name": "mac", "address": "u@192.0.2.1"}
        client = Client(object(), reverse_data_provider=lambda: {
            "hosts": [host]})
        client._open_reverse_for_host(host)
        labels = [row["label"] for row in client._reverse_rows()]
        self.assertTrue(any("SSH 접속" in label for label in labels))
        self.assertTrue(any("리버스 에이전트" in label for label in labels))
        self.assertTrue(any("키·연결 확인" in label for label in labels))
        client.reverse_sel = 0
        result = client._reverse_activate()
        self.assertEqual(result.action, "ssh-connect")
        # 리버스 행 -> provider 단계, q는 메뉴로 돌아온다
        client._open_reverse_for_host(host)
        client.reverse_sel = 1
        client._reverse_activate()
        self.assertEqual(client.reverse_stage, "provider")
        client._reverse_key(b"q")
        self.assertEqual(client.reverse_stage, "menu")
        self.assertTrue(client.reverse_open)

    def test_key_panel_flow(self):
        import sys
        sys.path.insert(0, str(REPO))
        from crowcli.client import Client
        from crowcli.vt import Screen
        key = {"name": "work", "path": "/k/crow_work", "type": "ED25519",
               "sha256": "SHA256:abcdefghijklmnop", "managed": True,
               "origin": "관리됨", "hosts": []}
        host = {"name": "mac", "address": "u@192.0.2.1"}
        calls = []
        client = Client(object(), sidebar_data_provider=lambda: {
            "sessions": [], "hosts": [host], "keys": [key]},
            key_actions={
                "list": lambda: [key],
                "copy": lambda k: calls.append(("copy", k["name"])) or "ok",
                "delete": lambda k, f: calls.append(("delete", f)) or "gone"})
        client._winsize = lambda: (100, 30)
        client._refresh_sidebar()
        client._sidebar_chunk()
        row = next(r for r, (kind, _) in client._sidebar_rows
                   if kind == "key")
        client._sidebar_click(row + 1)
        self.assertEqual(client.reverse_stage, "key")
        screen = Screen(100, 30)
        screen.feed(client._reverse_chunk())
        text = "\n".join("".join(c[0] for c in r) for r in screen.grid)
        self.assertIn("SSH 키", text)
        self.assertIn("SHA256:abcdefghijklmnop", text)
        client._reverse_activate()  # copy
        self.assertEqual(calls, [("copy", "work")])
        client.reverse_sel = 1  # install -> host picker -> result
        client._reverse_activate()
        self.assertEqual(client.reverse_stage, "key-host")
        result = client._reverse_activate()
        self.assertEqual(result.action, "key-install")
        self.assertEqual(result.arg, (key, host))
        client._open_keys(key)
        client.reverse_sel = 3  # 삭제… -> confirm -> files
        client._reverse_activate()
        self.assertEqual(client.reverse_stage, "key-delete")
        client._reverse_key(b"q")
        self.assertEqual(client.reverse_stage, "key")
        client._reverse_key(b"q")
        self.assertEqual(client.reverse_stage, "keys")
        labels = [r["label"] for r in client._reverse_rows()]
        self.assertTrue(labels[0].startswith("+ 새 키 만들기"))
        self.assertIn("관리 키", labels)
        client.reverse_sel = 0
        self.assertEqual(client._reverse_activate().action, "key-generate")
        client._open_keys(key)
        client.reverse_sel = 3
        client._reverse_activate()
        client.reverse_sel = 1
        client._reverse_activate()
        self.assertEqual(calls[-1], ("delete", True))
        self.assertEqual(client.reverse_stage, "keys")


if __name__ == "__main__":
    unittest.main()
