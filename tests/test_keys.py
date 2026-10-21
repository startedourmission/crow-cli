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


if __name__ == "__main__":
    unittest.main()
