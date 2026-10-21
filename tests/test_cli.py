"""CLI end-to-end: sessions persist across detach/reattach (same PID),
windows switch by registry, kill terminates the daemon."""
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import tempfile
import termios
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
os.environ.setdefault("CROWCLI_NO_IME_SWITCH", "1")
CROW = str(REPO / "crow")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="cch-", dir="/tmp")
        self.root = tempfile.TemporaryDirectory(prefix="ccr-", dir="/tmp")
        self.env = dict(os.environ, CROWCLI_HOME=self.home.name,
                        CROWCLI_SESSION_ROOT=self.root.name,
                        SHELL="/bin/sh", TERM="xterm-256color")

    def tearDown(self):
        self.cli("kill-session", "-t", "demo", check=False)
        self.home.cleanup()
        self.root.cleanup()

    def cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        result = subprocess.run([CROW] + list(args), env=self.env,
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            raise AssertionError("%s failed: %s%s" % (
                args, result.stdout, result.stderr))
        return result

    def attach(self, steps, session="demo"):
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen([CROW, "attach", "-t", session],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=self.env)
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

        drain(1.5)
        for data, wait in steps:
            os.write(master, data)
            drain(wait)
        proc.wait(timeout=10)
        os.close(master)
        return out

    def test_detached_session_lifecycle(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        listing = self.cli("ls").stdout
        self.assertIn("demo", listing)
        self.assertIn("running", listing)
        self.assertIn(str(REPO), self.cli("info", "-t", "demo").stdout)
        self.cli("new-window", "-t", "demo", "-n", "second", "--",
                 "exec /bin/sh")
        self.assertIn("second", self.cli("list-windows", "-t",
                                         "demo").stdout)
        self.cli("kill-session", "-t", "demo")
        self.assertIn("no sessions", self.cli("ls").stdout)

    def test_attach_detach_reattach_same_process(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        first = self.attach([(b"printf '__PID_%s__\\n' $$\n", 1.0),
                             (b"\x02d", 1.0)])
        pid = re.search(rb"__PID_(\d+)__", first).group(1)
        # Output keeps being produced while detached; reattach replays it
        # and lands in the same shell process.
        second = self.attach([(b"printf '__AGAIN_%s__\\n' $$\n", 1.0),
                              (b"\x02d", 1.0)])
        self.assertIn(b"__PID_" + pid + b"__", second)  # replay
        self.assertIn(b"__AGAIN_" + pid + b"__", second)

    def test_status_bar_is_drawn_on_the_last_row(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        out = self.attach([(b"printf '__SIZE__'; stty size\n", 1.0),
                           (b"echo one\n", 0.6),
                           (b"echo two\n", 0.6),
                           (b"echo three\n", 0.6),
                           (b"\x02d", 1.0)])
        # The bar paints the real last row (24) and names the session,
        # while the child PTY is one row shorter (23).
        self.assertIn(b"\x1b[24;1H", out)
        self.assertIn(b" demo | ", out)
        self.assertIn(b"\x1b[0;1;48;5;45;38;5;16m 0:win0 ", out)  # tab
        self.assertIn(b"C-b d detach", out)
        self.assertIn(b"__SIZE__23 52", out)  # 80 - sidebar(27) - divider
        # The left sidebar is always drawn with sessions + hosts.
        self.assertNotIn(b"CROW-CLI", out)
        self.assertIn("세션".encode(), out)
        self.assertIn("호스트".encode(), out)
        self.assertIn("실행 중".encode(), out)
        # The last row is reserved on the outer terminal (scroll region
        # stops at row 23) and the bar is repainted while typing, not
        # just once at attach.
        self.assertIn(b"\x1b[1;23r", out)
        self.assertGreaterEqual(out.count(b" 0:win0 "), 4)

    def test_window_switch_prefix(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        self.cli("new-window", "-t", "demo", "-n", "other", "--",
                 "exec /bin/sh")
        out = self.attach([(b"\x02p", 1.0),  # previous window
                           (b"printf '__IN_%s__\\n' first\n", 1.0),
                           (b"\x02d", 1.0)])
        self.assertIn(b"__IN_first__", out)

    @unittest.skipUnless(os.path.exists("/bin/zsh"), "needs zsh")
    def test_zsh_panes_have_no_eol_percent_marks(self):
        # zsh prompt_sp stamps an inverse '%' after command output; in a
        # pane it must be suppressed via PROMPT_EOL_MARK (spawn_pane env).
        self.env = dict(self.env, HOME=self.home.name)
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/zsh", "-i")
        self.attach([(b"ls /\r", 1.2), (b"\x02d", 0.5)])
        out = subprocess.run([CROW, "capture", "-t", "demo"], env=self.env,
                             capture_output=True, timeout=30).stdout
        self.assertNotIn(b"[7m%", out)
        self.assertIn(b"Applications", out)


if __name__ == "__main__":
    unittest.main()

