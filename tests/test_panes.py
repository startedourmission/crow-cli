"""Pane splits: layout maths, VT screen, and live attach compositing —
two shells visible at once, input routed to the focused pane."""
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import tempfile
import termios
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
CROW = str(REPO / "crow")
import sys
sys.path.insert(0, str(REPO))
from crowcli.client import compute_rects  # noqa: E402
from crowcli.vt import Screen  # noqa: E402


class LayoutTests(unittest.TestCase):
    def test_vertical_and_horizontal_splits(self):
        panes = {"a": {}, "b": {}, "c": {}}
        tree = {"split": "v", "ratio": 0.5,
                "a": {"leaf": "a"},
                "b": {"split": "h", "ratio": 0.5,
                      "a": {"leaf": "b"}, "b": {"leaf": "c"}}}
        rects = compute_rects(tree, panes, 0, 0, 80, 23)
        self.assertEqual(rects["a"][1:], (0, 39, 23))
        self.assertEqual(rects["b"][0], 41)  # after 2-col divider
        self.assertEqual(rects["c"][1], rects["b"][1] + rects["b"][3] + 2)
        widths = rects["a"][2] + 2 + rects["b"][2]
        self.assertEqual(widths, 80)


class VtTests(unittest.TestCase):
    def text(self, screen, row=0):
        return "".join(cell[0] for cell in screen.grid[row]).rstrip()

    def test_print_cursor_and_erase(self):
        screen = Screen(10, 3)
        screen.feed(b"hello\x1b[1;3HX\x1b[K")
        self.assertEqual(self.text(screen), "heX")
        screen.feed(b"\x1b[2;1Hworld")
        self.assertEqual(self.text(screen, 1), "world")

    def test_alt_screen_restores_main(self):
        screen = Screen(10, 2)
        screen.feed(b"main\x1b[?1049halt\x1b[?1049l")
        self.assertEqual(self.text(screen), "main")

    def test_scroll_keeps_scrollback(self):
        screen = Screen(5, 2)
        screen.feed(b"one\r\ntwo\r\nthree")
        self.assertEqual(self.text(screen, 1), "three")
        self.assertTrue(any("one" in "".join(c[0] for c in line)
                            for line in screen.scrollback))

    def test_prompt_sp_mark_filtered_on_width_mismatch(self):
        # zsh prompt_sp: '%' + fill-to-EOL spaces + CR space CR. Recorded
        # at a wider width than the screen, the fill wraps and used to
        # leave a stray '%' line after every prompt.
        screen = Screen(10, 6)
        screen.feed(b"hello\r\n")
        screen.feed(b"\x1b[1m\x1b[7m%\x1b[27m\x1b[1m\x1b[0m"
                    + b" " * 19 + b"\r \r")
        screen.feed(b"PROMPT$ ls\r\nok\r\n")
        texts = ["".join(c[0] for c in line).rstrip()
                 for line in screen.lines_for_view(0)]
        texts += ["".join(c[0] for c in line).rstrip()
                  for line in screen.scrollback]
        self.assertNotIn("%", texts)
        self.assertIn("PROMPT$ ls", texts)

    def test_plain_percent_line_survives(self):
        screen = Screen(10, 6)
        screen.feed(b"%\r\ndone\r\n")
        texts = ["".join(c[0] for c in line).rstrip()
                 for line in screen.lines_for_view(0)]
        self.assertIn("%", texts)

    def test_sgr_and_wide_chars(self):
        screen = Screen(10, 1)
        screen.feed("\x1b[31mab\x1b[0m가나".encode())
        self.assertEqual(screen.grid[0][0][1][0], ("p", 1))
        self.assertEqual(screen.grid[0][2][0], "가")
        self.assertEqual(screen.grid[0][3][0], "")  # wide continuation

    def test_replay_skips_to_last_clear_only_in_alt_screen(self):
        def text(screen):
            return ["".join(c[0] for c in row).rstrip()
                    for row in screen.grid]
        junk = b"\x1b[31mjunk \xed\x95\x9c\xea\xb8\x80\r\n" * 40000
        alt = (b"\x1b[?1049h\x1b[?1h\x1b[?1002h" + junk
               + b"\x1b[2J\x1b[Hhello \xed\x95\x9c\xea\xb8\x80")
        fast, full = Screen(20, 5), Screen(20, 5)
        fast.feed_replay(alt)
        full.feed(alt)
        self.assertEqual(text(fast), text(full))
        self.assertEqual(text(fast)[0], "hello 한글")
        self.assertTrue(fast.alt and fast.app_cursor)
        self.assertEqual(fast.mouse_mode, full.mouse_mode)
        shell = junk + b"\x1b[2J\x1b[Hprompt"  # main screen: no skip
        fast, full = Screen(20, 5), Screen(20, 5)
        fast.feed_replay(shell)
        full.feed(shell)
        self.assertEqual(len(fast.scrollback), len(full.scrollback))

    def test_fast_path_matches_wrapping_and_wide_text(self):
        screen = Screen(5, 3)
        screen.feed("abcdefg\x1b[2;1H가나다\x1b[1;3Hz".encode())
        rows = ["".join(c[0] for c in row) for row in screen.grid]
        self.assertEqual(rows[0], "abzde")
        self.assertEqual(rows[1], "가나다")  # "fg" wrapped, overwritten
        self.assertEqual((screen.cx, screen.cy), (3, 0))

    def test_colour_queries_get_the_outer_terminals_colours(self):
        screen = Screen(10, 2)
        screen.feed(b"\x1b]11;?\x07")
        self.assertEqual(screen.responses, [])  # unknown: stay silent
        Screen.term_colors = {"10": "rgb:0000/0000/0000",
                              "11": "rgb:ffff/ffff/ffff"}
        try:
            screen.feed(b"\x1b]11;?\x07ab\x1b]10;?\x1b\\\x1b]0;title\x07")
        finally:
            Screen.term_colors = {}
        self.assertEqual(screen.responses, [
            b"\x1b]11;rgb:ffff/ffff/ffff\x07",
            b"\x1b]10;rgb:0000/0000/0000\x1b\\"])
        self.assertEqual("".join(c[0] for c in screen.grid[0][:2]), "ab")

    def test_keyboard_mode_sequences_are_not_styles(self):
        screen = Screen(10, 1)
        screen.feed(b"ab\x1b[s\x1b[>4;2m\x1b[>4m\x1b[>1u\x1b[<u\x1b[>0qcd")
        self.assertEqual("".join(c[0] for c in screen.grid[0][:4]), "abcd")
        self.assertFalse(any(c[1][5] for c in screen.grid[0]))  # underline
        self.assertEqual(screen.responses, [])


class PaneAttachTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="cch-", dir="/tmp")
        self.root = tempfile.TemporaryDirectory(prefix="ccr-", dir="/tmp")
        self.env = dict(os.environ, CROWCLI_HOME=self.home.name,
                        CROWCLI_SESSION_ROOT=self.root.name,
                        SHELL="/bin/sh", TERM="xterm-256color")

    def tearDown(self):
        subprocess.run([CROW, "kill-session", "-t", "demo"], env=self.env,
                       capture_output=True, timeout=30)
        self.home.cleanup()
        self.root.cleanup()

    def cli(self, *args):
        result = subprocess.run([CROW] + list(args), env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                         "%s: %s" % (args, result.stderr))
        return result

    def attach(self, steps):
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen([CROW, "attach", "-t", "demo"],
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

    def test_split_by_prefix_and_focus_routing(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        out = self.attach([
            (b"echo MARKER_LEFT_$((40+2))\n", 1.0),
            (b"\x02%", 2.0),                      # split left/right
            (b"echo MARKER_RIGHT_$((10+5))\n", 1.0),
            (b"\x02\x1b[D", 1.5),                 # focus left (prefix+arrow)
            (b"echo BACK_LEFT_$((1+1))\n", 1.0),
            (b"\x02d", 1.0),
        ])
        self.assertIn(b"MARKER_LEFT_42", out)
        self.assertIn(b"MARKER_RIGHT_15", out)
        self.assertIn(b"BACK_LEFT_2", out)
        self.assertIn("│".encode(), out)          # pane divider drawn
        listing = self.cli("ls").stdout
        self.assertIn("2 pane(s)", listing)

    def test_both_panes_render_together_on_attach(self):
        self.cli("new", "-d", "-s", "demo", "--", "exec /bin/sh")
        self.cli("split-window", "-t", "demo")
        out = self.attach([
            (b"echo ONLY_IN_ACTIVE_$((7*7))\n", 1.0),
            (b"\x02d", 1.0),
        ])
        # The inactive pane's shell prompt and the active pane's marker
        # are composited in the same attach rendering.
        self.assertIn(b"ONLY_IN_ACTIVE_49", out)
        self.assertIn("│".encode(), out)
        # Pane sizes: 80 cols split with a 1-col divider, 23 rows high.
        out2 = self.attach([
            (b"printf '__SZ__'; stty size\n", 1.0),
            (b"\x02d", 1.0),
        ])
        self.assertIn(b"__SZ__23 ", out2)
        self.assertNotIn(b"__SZ__23 80", out2)


if __name__ == "__main__":
    unittest.main()
