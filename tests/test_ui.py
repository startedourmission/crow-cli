"""Mouse input, the status-bar Files button / explorer, and saved hosts."""
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
CROW = str(REPO / "crow")
os.environ.setdefault("CROWCLI_NO_IME_SWITCH", "1")


class UiTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="cch-", dir="/tmp")
        self.root = tempfile.TemporaryDirectory(prefix="ccr-", dir="/tmp")
        self.ws = tempfile.TemporaryDirectory(prefix="ccw-", dir="/tmp")
        Path(self.ws.name, "note.txt").write_text("hello explorer\n")
        Path(self.ws.name, "sub").mkdir()
        self.editor_marker = Path(self.ws.name) / "edited.txt"
        editor = Path(self.ws.name) / "fake-editor.sh"
        editor.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" >> '"
                          + str(self.editor_marker) + "'\nexec /bin/sh\n")
        editor.chmod(0o700)
        self.env = dict(os.environ, CROWCLI_HOME=self.home.name,
                        CROWCLI_SESSION_ROOT=self.root.name,
                        SHELL="/bin/sh", TERM="xterm-256color",
                        EDITOR=str(editor))

    def tearDown(self):
        subprocess.run([CROW, "kill-session", "-t", "demo"], env=self.env,
                       capture_output=True, timeout=30)
        self.home.cleanup()
        self.root.cleanup()
        self.ws.cleanup()

    def cli(self, *args, **kwargs):
        check = kwargs.pop("check", True)
        result = subprocess.run([CROW] + list(args), env=self.env,
                                capture_output=True, text=True, timeout=30)
        if check and result.returncode != 0:
            raise AssertionError("%s: %s" % (args, result.stderr))
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

    def test_saved_hosts_crud(self):
        self.cli("host", "add", "mac", "--address", "me@10.0.0.5",
                 "--port", "2222")
        listing = self.cli("hosts").stdout
        self.assertIn("mac: me@10.0.0.5", listing)
        self.assertIn("port=2222", listing)
        self.cli("host", "rm", "mac")
        self.assertIn("no saved hosts", self.cli("hosts").stdout)
        # One field is enough: the name is derived, key defaults to ssh's.
        self.cli("host", "add", "jinwoo@10.0.0.9")
        listing = self.cli("hosts").stdout
        self.assertIn("jinwoo@10.0.0.9", listing)
        self.assertIn("key=기본(자동)", listing)
        self.cli("host", "edit", "10.0.0.9", "--address", "root@10.0.0.10",
                 "--port", "2200", "--identity", "~/.ssh/other",
                 "--name", "box")
        listing = self.cli("hosts").stdout
        self.assertIn("box: root@10.0.0.10 port=2200 key=~/.ssh/other",
                      listing)
        self.cli("host", "edit", "box", "--default-port", "--default-key")
        self.assertIn("box: root@10.0.0.10 key=기본(자동)",
                      self.cli("hosts").stdout)
        self.assertNotEqual(self.cli("host", "edit", "nope", "--port", "1",
                                     check=False).returncode, 0)
        self.cli("host", "rm", "box")
        self.assertIn("no saved hosts", self.cli("hosts").stdout)

    def test_window_switch_keeps_background_screens_current(self):
        from crowcli.vt import Screen
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "echo ZERO; exec /bin/sh")
        self.cli("new-window", "-t", "demo", "-c", self.ws.name, "--",
                 "i=0; while [ $i -lt 400 ]; do i=$((i+1)); "
                 "echo tick-$i; sleep 0.05; done; exec /bin/sh")
        self.cli("select-window", "-t", "demo:0")
        out = self.attach([
            (b"\x02n", 1.0),   # window 1 (fresh replay)
            (b"\x02n", 2.0),   # back to 0; window 1 keeps ticking parked
            (b"\x02n", 0.3),   # window 1 again, served from the cache
            (b"\x02d", 1.0),
        ])
        screen = Screen(80, 24)
        # Only the frames painted after the last switch matter.
        screen.feed(out[:out.rfind(b"\x1b[?1049l")])
        text = "\n".join("".join(c[0] for c in row) for row in screen.grid)
        ticks = [int(t) for t in re.findall(r"tick-(\d+)", text)]
        self.assertTrue(ticks, text)
        # ~3.3s at 20 ticks/s: the parked screen saw the 2s it was away.
        self.assertGreater(max(ticks), 45, text)
        self.assertNotIn("ZERO", text)

    def test_window_switch_does_not_toggle_alternate_screen(self):
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        self.cli("new-window", "-t", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        out = self.attach([(b"\x02n", 0.8), (b"\x02n", 0.8),
                           (b"\x02d", 1.0)])
        self.assertEqual(out.count(b"\x1b[?1049h"), 1)
        self.assertEqual(out.count(b"\x1b[?1049l"), 1)  # only at detach
        self.assertTrue(out.rstrip().endswith(b"(still running)"), out[-200:])

    def test_sighup_dumps_flight_recorder(self):
        import signal
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "echo RECORDED-OUTPUT; exec /bin/sh")
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen([CROW, "attach", "-t", "demo"],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=self.env)
        os.close(slave)
        end = time.time() + 2
        while time.time() < end:
            if select.select([master], [], [], 0.05)[0]:
                os.read(master, 65536)
        os.write(master, b"\x1b[<0;40;5M")
        time.sleep(0.3)
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=10)
        os.close(master)
        crash = Path(self.home.name) / "crash"
        outs = list(crash.glob("*.out"))
        self.assertEqual(len(outs), 1)
        self.assertIn(b"RECORDED-OUTPUT", outs[0].read_bytes())
        log = outs[0].with_suffix(".log").read_text()
        self.assertIn("reason: SIGHUP", log)
        self.assertIn("attach", log)
        self.assertIn("[<0;40;5M", log)
        self.assertEqual(oct(outs[0].stat().st_mode & 0o777), "0o600")

    def test_terminal_vanishing_dumps_flight_recorder(self):
        # A crashed terminal app closes the pty: the client sees EOF/EIO,
        # often before any SIGHUP, and must still keep the record.
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "echo VANISHED-OUTPUT; exec /bin/sh")
        master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen([CROW, "attach", "-t", "demo"],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=self.env, start_new_session=True)
        os.close(slave)
        end = time.time() + 2
        while time.time() < end:
            if select.select([master], [], [], 0.05)[0]:
                os.read(master, 65536)
        os.close(master)
        proc.wait(timeout=10)
        outs = list((Path(self.home.name) / "crash").glob("*.out"))
        self.assertEqual(len(outs), 1)
        self.assertIn(b"VANISHED-OUTPUT", outs[0].read_bytes())

    def test_mouse_click_focuses_pane_and_wheel_does_not_crash(self):
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        self.cli("split-window", "-t", "demo")
        click_left = b"\x1b[<0;35;5M\x1b[<0;35;5m"  # first pane (sidebar is 0-28)
        wheel_up = b"\x1b[<64;50;5M"
        out = self.attach([
            (b"seq 1 40\n", 1.2),
            (wheel_up, 0.8),
            (click_left, 0.8),
            (b"echo CLICKED_LEFT_$((3*3))\n", 1.0),
            (b"\x02d", 1.0),
        ])
        self.assertIn(b"\x1b[?1002h\x1b[?1006h", out)  # mouse reporting on
        self.assertIn(b"CLICKED_LEFT_9", out)

    def test_files_button_opens_explorer_and_file_in_editor_pane(self):
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        click_files = b"\x1b[<0;10;24M\x1b[<0;10;24m"  # after " Hosts "
        out = self.attach([
            (click_files, 1.2),
            (b"\x1b[B\x1b[B\x1b[B", 0.8),   # .., sub/, fake-editor.sh -> note.txt
            (b"\r", 2.0),            # open file in an editor pane
            (b"\x02d", 1.0),
        ])
        self.assertIn(" Files ".encode(), out)
        self.assertIn(b"Files:", out)
        self.assertIn(b"note.txt", out)
        self.assertTrue(self.editor_marker.exists(),
                        "file was not opened in the editor pane")
        self.assertIn("note.txt", self.editor_marker.read_text())
        self.assertIn("2 pane(s)", self.cli("ls").stdout)

    def test_reverse_panel_starts_agent_and_switches(self):
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        click_bar = b"\x1b[<0;15;24M\x1b[<0;15;24m"  # old [리버스] spot
        out = self.attach([
            (click_bar, 0.6),
            (b"\x02a", 1.2),
            (b"\x02d", 1.0),
        ])
        self.assertIn("리버스".encode(), out)
        self.assertIn("호스트 · 리버스".encode(), out)  # merged sidebar tab
        self.assertIn("저장된 호스트가 없습니다".encode(), out)
        self.assertNotIn("로컬 샌드박스 실행".encode(), out)
        # The local fake-agent flow lives on the CLI (--local), not the UI.
        self.cli("reverse-agent", "--local", "--provider", "fake",
                 "-s", "agent-fake", "-d", "--dir", self.ws.name)
        captured = b""
        deadline = time.time() + 10
        while time.time() < deadline and \
                b"client workspace is" not in captured:
            captured = subprocess.run(
                [CROW, "capture", "-t", "agent-fake"], env=self.env,
                capture_output=True, timeout=30).stdout
            time.sleep(0.2)
        self.assertIn(b"client workspace is", captured)
        target = Path(self.ws.name) / "reverse-agent-was-here.txt"
        deadline = time.time() + 10
        while not target.exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(target.exists())
        self.assertIn("agent-fake", self.cli("ls").stdout)
        subprocess.run([CROW, "kill-session", "-t", "agent-fake"],
                       env=self.env, capture_output=True, timeout=30)

    def test_prefix_e_toggles_explorer(self):
        self.cli("new", "-d", "-s", "demo", "-c", self.ws.name, "--",
                 "exec /bin/sh")
        out = self.attach([
            (b"\x02e", 1.2),
            (b"q", 0.8),
            (b"\x02d", 1.0),
        ])
        self.assertIn(b"Files:", out)
        self.assertIn(b"sub/", out)


if __name__ == "__main__":
    unittest.main()


class FailureDisplayTests(unittest.TestCase):
    """Failures must be visible: reason in the sidebar, red, with a notice."""

    def test_probe_error_classification(self):
        from crowcli.client import Client
        cases = {
            "Permission denied (publickey).": "연결 실패 (인증 오류)",
            "Host key verification failed.": "연결 실패 (호스트 키)",
            "ssh: connect to host x: Operation timed out": "연결 실패 (시간 초과)",
            "Could not resolve hostname x": "연결 실패 (주소 확인 불가)",
            "Connection refused": "연결 실패 (연결 거부)",
            "whatever": "연결 실패",
        }
        for stderr, expected in cases.items():
            self.assertEqual(Client.classify_probe_error(stderr), expected)

    def test_focus_colours_only_the_focused_panes_edge(self):
        from crowcli.client import Client, compute_rects
        from crowcli.vt import Screen
        client = Client(object())
        client._winsize = lambda: (80, 24)
        tree = {"split": "v", "ratio": 0.5,
                "a": {"leaf": "a"}, "b": {"leaf": "b"}}
        rects = compute_rects(tree, {"a": {}, "b": {}}, *client._area())
        left_edge = rects["a"][0] + rects["a"][2]  # a's own line
        right_edge = rects["b"][0] - 1              # b's own line
        self.assertEqual(right_edge - left_edge, 1)  # two lines, 2 cells

        def colours(active):
            client.active_id = active
            screen = Screen(80, 24)
            screen.feed(client._borders(rects))
            row = screen.grid[5]
            return row[left_edge][1][0], row[right_edge][1][0]

        cyan = ("p", 51)
        self.assertEqual(colours("a"), (cyan, ("p", 240)))
        self.assertEqual(colours("b"), (("p", 240), cyan))

    def test_sidebar_shows_failure_in_red_with_notice(self):
        from crowcli.client import Client
        client = Client(
            object(),
            sidebar_data_provider=lambda: {
                "sessions": [{"name": "agent-claude",
                              "status": "연결 실패", "current": False}],
                "hosts": [{"name": "mac", "address": "u@bad"}]},
            notice_provider=lambda: "리버스 시작 실패: 인증 오류")
        client._host_status["u@bad"] = {
            "state": "연결 실패 (인증 오류)", "color": "31",
            "at": time.time()}
        out = client._sidebar_chunk()
        self.assertIn("연결 실패 (인증 오류)".encode(), out)
        self.assertIn("리버스 시작 실패".encode(), out)
        self.assertIn(b"48;5;160", out)  # red background
        self.assertIn("✗".encode(), out)


class HelpPopupTests(unittest.TestCase):
    def test_prefix_question_opens_popup_and_any_key_closes(self):
        from crowcli.client import Client
        from crowcli.vt import Screen
        client = Client(object())
        client._winsize = lambda: (100, 30)
        self.assertIsNone(client._input(b"\x02?", {}))  # stays attached
        self.assertTrue(client.help_open)
        client._help_opened_at = 0  # skip the pop-in
        screen = Screen(100, 30)
        screen.feed(client._help_chunk())
        text = "\n".join("".join(c[0] for c in row) for row in screen.grid)
        self.assertIn("crow-cli 단축키", text)
        self.assertIn("좌우로 분할", text)
        self.assertIn("아무 키나 누르면 닫힘", text)
        client._input(b"\x1b[<0;5;5m", {})  # mouse release: still open
        self.assertTrue(client.help_open)
        self.assertIsNone(client._input(b"q", {}))  # swallowed, not sent
        self.assertFalse(client.help_open)


class HostFormTests(unittest.TestCase):
    def make_client(self, saved):
        from crowcli.client import Client

        def save(address, port, identity, original=None, name=None):
            if port == "0":
                raise ValueError("포트는 1~65535 사이 숫자여야 해요")
            saved.append((address, port, identity) +
                         ((original,) if original else ()) +
                         ((name,) if name else ()))
            return {"name": name or original or "box", "address": address}

        def delete(host):
            saved.append(("deleted", host["name"]))
            return "%s 삭제함" % host["name"]
        client = Client(object(), host_form={
            "keys": lambda: [("~/.ssh/id_ed25519", "/k/id_ed25519")],
            "derive": lambda a: "box", "save": save, "delete": delete})
        client._winsize = lambda: (100, 30)
        client._toggle_reverse = lambda: setattr(
            client, "reverse_open", not client.reverse_open)
        return client

    def test_form_saves_and_opens_host_menu(self):
        from crowcli.vt import Screen
        saved = []
        client = self.make_client(saved)
        client._open_host_form()
        client.form["opened"] = 0
        for unit in [b"r", b"o", b"o", b"t", b"@", b"b", b"x", b"\x7f",
                     b"o", b"x", b"\t", b"\t", b"a", b"0", b"\t",
                     b"\x1b[C"]:
            self.assertIsNone(client._input(unit, {}))
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        text = "\n".join("".join(c[0] for c in row) for row in screen.grid)
        self.assertIn("호스트 추가", text)
        self.assertIn("root@box", text)
        self.assertIn("id_ed25519", text)
        client._input(b"\r", {})
        self.assertEqual(saved, [])  # port "0" rejected, form stays
        self.assertIn("1~65535", client.form["error"])
        client.form["port"] = "2222"
        client._input(b"\r", {})
        self.assertEqual(saved, [("root@box", "2222", "/k/id_ed25519")])
        self.assertIsNone(client.form)
        self.assertTrue(client.reverse_open)
        self.assertEqual(client.reverse_stage, "menu")

    def pick(self, client, kind):
        rows = client._reverse_rows()
        client.reverse_sel = [r["kind"] for r in rows].index(kind)
        return client._input(b"\r", {})

    def test_edit_prefills_and_keeps_name(self):
        from crowcli.vt import Screen
        saved = []
        client = self.make_client(saved)
        host = {"name": "mac", "address": "me@10.0.0.5", "port": 2222,
                "identity": "/k/id_ed25519"}
        client._open_reverse_for_host(host)
        self.pick(client, "host-edit")
        self.assertEqual((client.form["address"], client.form["port"]),
                         ("me@10.0.0.5", "2222"))
        self.assertEqual(client.form["keys"][client.form["key"]][1],
                         "/k/id_ed25519")
        client.form["opened"] = 0
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        text = "\n".join("".join(c[0] for c in row) for row in screen.grid)
        self.assertIn("호스트 수정", text)
        self.assertIn("mac", text)
        self.assertEqual(client.form["name"], "mac")
        client._input(b"\x7f", {})
        client._input(b"6", {})
        client._input(b"\r", {})
        self.assertEqual(saved, [("me@10.0.0.6", "2222", "/k/id_ed25519",
                                  "mac", "mac")])
        self.assertEqual(client.reverse_stage, "menu")
        self.assertEqual(client._pending_host["name"], "mac")

    def test_alias_field(self):
        from crowcli.vt import Screen
        saved = []
        client = self.make_client(saved)
        client._open_host_form()
        client.form["opened"] = 0
        client._input(b"root@10.0.0.5\t", {})
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        text = "\n".join("".join(c[0] for c in row) for row in screen.grid)
        self.assertIn("별칭", text)
        self.assertIn("box (주소에서 자동)", text)
        client._input("회사 서버".encode(), {})  # spaces are dropped
        client._input(b"\r", {})
        self.assertEqual(saved, [("root@10.0.0.5", "", None, "회사서버")])
        self.assertEqual(client._pending_host["name"], "회사서버")

    def test_caret_moves_and_edits_mid_text(self):
        from crowcli.vt import Screen
        client = self.make_client([])
        client._open_host_form()
        client.form["opened"] = 0
        for unit in [b"r", b"x", b"t", b"@", b"b", b"x"] + [b"\x1b[D"] * 5 \
                + [b"\x1b[3~", b"o", b"o"]:
            client._input(unit, {})
            self.assertTrue(client._focus_dirty)  # repaint right away
        self.assertEqual(client.form["address"], "root@bx")
        self.assertEqual(client.form["caret"]["address"], 3)
        client._input(b"\x7f", {})
        client._input(b"o", {})
        self.assertEqual(client.form["address"], "root@bx")
        client._input(b"\x1b[F", {})
        for _ in range(2):
            client._input(b"\x7f", {})
        client._input(b"x", {})
        self.assertEqual(client.form["address"], "root@x")
        client.form["address"] = "root@bx"
        client.form["caret"]["address"] = 7
        client._input(b"\x01", {})  # ^A, then backspace at 0 is a no-op
        client._input(b"\x7f", {})
        client._input(b"\x1b[C", {})
        client._input(b"\x7f", {})
        self.assertEqual(client.form["address"], "oot@bx")
        client._input(b"\x1b[F", {})
        client._input(b"!", {})
        self.assertEqual(client.form["address"], "oot@bx!")
        client._input(b"\x1b[H", {})
        client._input(b"\x1b[C", {})
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        row, col = client.form["cursor"]
        cells = [c[0] for c in screen.grid[row - 1]]
        self.assertEqual("".join(cells[col - 1:col + 5]), "ot@bx!")
        # A click lands the caret under the pointer.
        click = ("\x1b[<0;%d;%dM" % (col + 3, row)).encode()
        client._input(click, {})
        self.assertEqual(client.form["caret"]["address"], 4)

    def test_long_address_scrolls_to_caret(self):
        from crowcli.vt import Screen
        client = self.make_client([])
        client._open_host_form()
        client.form["opened"] = 0
        long = "user@" + "a" * 80 + ".example.com"
        client._input(long.encode(), {})
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        row, col = client.form["cursor"]
        cells = [c[0] for c in screen.grid[row - 1]]
        self.assertTrue("".join(cells[:col - 1]).endswith("example.com"))
        client._input(b"\x01", {})
        screen = Screen(100, 30)
        screen.feed(client._form_chunk())
        row, col = client.form["cursor"]
        cells = [c[0] for c in screen.grid[row - 1]]
        self.assertEqual("".join(cells[col - 1:col + 4]), "user@")

    def test_edit_keeps_unlisted_key(self):
        client = self.make_client([])
        client._open_host_form({"name": "mac", "address": "a@b",
                                "identity": "~/.ssh/custom"})
        self.assertEqual(client.form["keys"][client.form["key"]],
                         ("custom", "~/.ssh/custom"))

    def test_delete_asks_then_removes(self):
        saved = []
        client = self.make_client(saved)
        client._flash = lambda text: saved.append(("flash", text))
        client._open_reverse_for_host({"name": "mac", "address": "a@b"})
        self.pick(client, "host-delete")
        self.assertEqual(client.reverse_stage, "host-delete")
        self.pick(client, "host-delete-no")
        self.assertEqual(client.reverse_stage, "menu")
        self.assertEqual(saved, [])
        self.pick(client, "host-delete")
        self.pick(client, "host-delete-yes")
        self.assertEqual(saved, [("deleted", "mac"), ("flash", "mac 삭제함")])
        self.assertFalse(client.reverse_open)
        self.assertIsNone(client._pending_host)

    def test_escape_cancels_and_empty_address_is_refused(self):
        saved = []
        client = self.make_client(saved)
        client._open_host_form()
        client._input(b"\r", {})
        self.assertIn("주소", client.form["error"])
        client._input(b"", {}, flush_esc=True)  # nothing buffered
        client._input(b"\x1b", {})
        client._input(b"", {}, flush_esc=True)
        self.assertIsNone(client.form)
        self.assertEqual(saved, [])

    def test_cancel_targets_button_hint_and_outside(self):
        from crowcli.vt import Screen

        def find(client, word, bottom=False):
            screen = Screen(100, 30)
            screen.feed(client._form_chunk())
            rows = range(29, -1, -1) if bottom else range(30)
            for y in rows:
                cols = [c[0] for c in screen.grid[y]]
                for x in range(len(cols)):
                    if "".join(cols[x:x + len(word)]) == word:
                        return x + 1, y + 1
            self.fail(word)

        def click(client, x, y):
            client._input(("\x1b[<0;%d;%dM" % (x, y)).encode(), {})

        for word, bottom in (("취", False), ("Esc", True)):
            client = self.make_client([])
            client._open_host_form()
            client.form["opened"] = 0
            click(client, *find(client, word, bottom))
            self.assertIsNone(client.form, word)
        client = self.make_client([])
        client._open_host_form()
        client.form["opened"] = 0
        client._form_chunk()
        self.assertTrue(client.form["full"])
        self.assertFalse(client._form_animating())
        click(client, 50, 15)  # inside, not on a target: stays open
        self.assertIsNotNone(client.form)
        click(client, 2, 2)
        self.assertIsNone(client.form)


class SidebarKeyboardTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from crowcli import imswitch
        patcher = mock.patch.multiple(imswitch, to_latin=lambda: None,
                                      restore=lambda _id: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_client(self):
        from crowcli.client import Client
        self.hosts = [{"name": "mac", "address": "u@192.0.2.1"},
                      {"name": "집", "address": "me@192.0.2.2"}]
        client = Client(object(), sidebar_data_provider=lambda: {
            "sessions": [{"name": "main", "current": True},
                         {"name": "work"}],
            "hosts": list(self.hosts), "keys": []},
            host_form={"keys": lambda: [], "derive": lambda a: a,
                       "save": None, "delete": None})
        client._winsize = lambda: (100, 30)
        client._host_state = lambda host: ("연결됨", "32")
        client._toggle_reverse = lambda: setattr(
            client, "reverse_open", not client.reverse_open)
        client._refresh_sidebar()
        self.addCleanup(setattr, Client, "sidebar_open", Client.sidebar_open)
        return client

    def press(self, client, *units):
        result = None
        for unit in units:
            client._sidebar_chunk()  # the loop repaints between keys
            result = client._input(unit, {})
        client._sidebar_chunk()
        return result

    def highlighted(self, client):
        import re
        chunk = client._sidebar_chunk().decode()
        return [text.strip() for text in re.findall(
            "\x1b\\[\\d+;1H\x1b\\[1;38;5;231;48;5;31m([^\x1b]*)", chunk)]

    def test_prefix_h_focuses_first_host_and_moves(self):
        client = self.make_client()
        self.press(client, b"\x02", b"h")
        self.assertTrue(client.sidebar_focus)
        self.assertEqual(self.highlighted(client), ["● mac", "연결됨"])
        self.press(client, b"j", b"j")
        self.assertEqual(self.highlighted(client), ["+ 호스트 추가…"])
        self.press(client, b"\x1b[A")
        self.assertEqual(self.highlighted(client)[0], "● 집")
        self.assertIn("Esc 나가기", client._bar_text()[1])
        self.press(client, b"\r")
        self.assertTrue(client.reverse_open)
        self.assertEqual(client._pending_host["name"], "집")
        self.press(client, b"q")  # closing the panel lands back here
        self.assertFalse(client.reverse_open)
        self.assertTrue(client.sidebar_focus)
        self.press(client, b"\x1b")
        client._input(b"", {}, flush_esc=True)
        self.assertFalse(client.sidebar_focus)

    def test_host_shortcuts_and_session_switch(self):
        client = self.make_client()
        self.press(client, b"\x02", b"h", b"e")
        self.assertEqual(client.form["edit"], "mac")
        client._close_host_form()
        self.press(client, b"d")
        self.assertEqual(client.reverse_stage, "host-delete")
        self.assertEqual(client._pending_host["name"], "mac")
        self.press(client, b"q", b"q")  # confirm -> host menu -> closed
        self.assertFalse(client.reverse_open)
        self.assertEqual(self.press(client, b"s").arg["name"], "mac")
        self.press(client, b"a")
        self.assertIsNone(client.form["edit"])
        client._close_host_form()
        self.press(client, b"k")  # wraps up into the session list
        result = self.press(client, b"\r")
        self.assertEqual(result.action, "switch-session")
        self.assertEqual(result.arg, "work")
        # Korean IME keys map to the same commands.
        self.press(client, "ㅓ".encode())
        self.assertEqual(self.highlighted(client)[0], "● mac")

    def test_deleted_host_keeps_selection_in_range(self):
        client = self.make_client()
        self.press(client, b"\x02", b"h", b"j")
        self.hosts.pop()
        client._sidebar_at = 0.0
        client._refresh_sidebar()
        self.assertEqual(self.highlighted(client)[0], "+ 호스트 추가…")

    def test_pane_click_leaves_sidebar(self):
        client = self.make_client()
        self.press(client, b"\x02", b"h")
        client._input(b"\x1b[<0;60;5M", {})
        self.assertFalse(client.sidebar_focus)


class ImagePasteTests(unittest.TestCase):
    def test_ctrl_v_uploads_clipboard_image_for_remote_panes(self):
        from unittest import mock
        from crowcli import clipimage
        from crowcli.client import Client, PaneRuntime
        sent = []

        class Conn(object):
            def send(self, kind, data):
                sent.append(data)
        uploads = []

        def target(pane_meta, window_meta):
            if not window_meta.get("remote_host"):
                return None
            return lambda data: uploads.append(data) or \
                "/tmp/crow-clipboard-x/image.png"
        client = Client(object(), image_target=target)
        client._winsize = lambda: (80, 24)
        local = PaneRuntime({"id": "p", "name": "pane0"}, {"meta": {}})
        local.conn = Conn()
        client.runtimes, client.active_id = [local], "p"
        client._input(b"\x16", {})
        self.assertEqual(sent, [b"\x16"])  # local: Ctrl-V passes through
        del sent[:]
        remote = PaneRuntime({"id": "p", "name": "pane0"},
                             {"meta": {"remote_host": "u@h"}})
        remote.conn = Conn()
        client.runtimes = [remote]
        with mock.patch.object(clipimage, "read_png",
                               return_value=clipimage.PNG_MAGIC + b"x"):
            client._input(b"\x16", {})
            for _ in range(100):
                if client._paste_queue:
                    break
                time.sleep(0.01)
        client._flush_pastes()
        self.assertEqual(uploads, [clipimage.PNG_MAGIC + b"x"])
        self.assertEqual(sent, [b"/tmp/crow-clipboard-x/image.png "])
        self.assertIn("이미지 첨부", client._bar_text(80)[0])
        with mock.patch.object(clipimage, "read_png", return_value=None):
            client._input(b"\x16", {})
            for _ in range(100):
                if client._paste_queue:
                    break
                time.sleep(0.01)
        client._flush_pastes()
        self.assertEqual(sent[-1], b"\x16")  # no image: ordinary Ctrl-V

    def test_pasted_path_quotes_unsafe_paths(self):
        from crowcli import clipimage
        self.assertEqual(clipimage.pasted_path("/tmp/a.png"), "/tmp/a.png ")
        self.assertEqual(clipimage.pasted_path("/tmp/it's x.png"),
                         "'/tmp/it'\\''s x.png' ")
        with self.assertRaises(ValueError):
            clipimage.validate(b"GIF89a")


class WindowTabTests(unittest.TestCase):
    def test_bar_shows_clickable_window_tabs(self):
        from crowcli.client import Client
        client = Client(object(), status_provider=lambda: " demo | shell ",
                        windows_provider=lambda: [("main", False),
                                                  ("logs", True),
                                                  ("build", False)])
        client._winsize = lambda: (100, 24)
        bar = client._bar_chunk()
        self.assertIn(b"\x1b[0;1;48;5;45;38;5;16m 1:logs ", bar)  # current
        self.assertIn(b" 0:main ", bar)
        tabs = [b for b in client._bar_buttons
                if b[2] not in ("explorer", "sidebar")]
        self.assertEqual([b[2] for b in tabs],
                         [("window", 0), ("window", 1), ("window", 2)])
        # Tabs follow the session block (one gap cell); hints sit flush right.
        session_end = len(" Hosts ▾") + len(" Files ") + len(" demo | shell ")
        self.assertEqual(tabs[0][0], session_end + 2)
        self.assertIn(client.SESSION_STYLE + b" demo | shell ", bar)
        self.assertTrue(bar.endswith(b"C-b ? help \x1b[0m"))
        x = tabs[2][0] + 1
        click = ("\x1b[<0;%d;24M\x1b[<0;%d;24m" % (x, x)).encode()
        result = client._input(click, {})
        self.assertEqual((result.action, result.arg), ("switch", ("index", 2)))

    def test_explorer_docks_beside_panes_and_shares_focus(self):
        from crowcli.client import Client, PaneRuntime
        sent = []

        class Conn(object):
            def send(self, kind, data):
                sent.append(data)
        client = Client(object())
        client._winsize = lambda: (120, 24)
        runtime = PaneRuntime({"id": "p", "name": "pane0", "cwd": "/tmp"},
                              {"meta": {}})
        runtime.conn = Conn()
        client.runtimes, client.active_id = [runtime], "p"
        client._toggle_explorer()
        width = client._sidebar_span()[0]
        self.assertEqual(client._area()[0], width + 1)  # panes after it + gap
        self.assertTrue(client.explorer_focus)
        client._input(b"j", {})
        self.assertEqual(sent, [])  # explorer has the keys
        rects = {"p": (width + 1, 0, 119 - width, 23)}
        client._input(("\x1b[<0;%d;5M" % (width + 10)).encode(), rects)
        self.assertTrue(client.explorer_open)  # still visible
        self.assertFalse(client.explorer_focus)
        client._input(b"j", rects)
        self.assertEqual(sent, [b"j"])  # terminal gets the keys
        client._input(b"\x1b[<0;5;5M", rects)  # click the explorer
        self.assertTrue(client.explorer_focus)

    def test_hosts_button_toggles_the_sidebar(self):
        from crowcli.client import Client
        client = Client(object())
        client._winsize = lambda: (100, 24)
        client._sidebar_data = {"sessions": [], "hosts": []}
        client._refresh_sidebar = lambda: None
        try:
            self.assertEqual(client._area()[0], 28)  # sidebar shown
            client._bar_chunk()
            hosts = [b for b in client._bar_buttons if b[2] == "sidebar"][0]
            click = ("\x1b[<0;%d;24M" % hosts[0]).encode()
            client._input(click, {})
            self.assertFalse(Client.sidebar_open)
            self.assertEqual(client._area()[0], 0)  # panes take the width
            self.assertEqual(client._sidebar_chunk(), b"")
            self.assertIn(b" Hosts ", client._bar_chunk())
            client._input(click, {})
            self.assertTrue(Client.sidebar_open)
            self.assertIn(b" Hosts \xe2\x96\xbe", client._bar_chunk())
            client.explorer_open = True  # Hosts takes the left side back
            client._input(click, {})
            self.assertFalse(client.explorer_open)
            self.assertTrue(Client.sidebar_open)
        finally:
            Client.sidebar_open = True


class PanelWidthTests(unittest.TestCase):
    """Korean labels are 2 cells wide: panel rows must not protrude."""

    @staticmethod
    def _display_width(chunk):
        import re as _re
        from crowcli.vt import char_width
        text = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", chunk.decode())
        return sum(char_width(c) for c in text)

    def test_reverse_panel_rows_are_exact_width(self):
        from crowcli.client import Client
        client = Client(object())
        client.reverse_open = True
        client._pending_local = True
        client.reverse_data = {"hosts": []}
        area_w = client._area()[2]
        width = min(40, area_w)
        area_h = client._area()[3]
        chunk = client._reverse_chunk()
        # title + subtitle + visible rows + bottom, each exactly `width`
        self.assertEqual(self._display_width(chunk), width * area_h)

    def test_explorer_panel_rows_are_exact_width(self):
        from crowcli.client import Client
        client = Client(object())
        client.explorer_open = True
        client.explorer_path = "/tmp/한글폴더"
        client.explorer_entries = [("긴한글이름파일.txt", False,
                                    "/tmp/한글폴더/긴한글이름파일.txt"),
                                   ("폴더", True, "/tmp/한글폴더/폴더")]
        width = client._sidebar_span()[0]  # docked in the left column
        area_h = client._area()[3]
        chunk = client._explorer_chunk()
        self.assertEqual(self._display_width(chunk), (width + 1) * area_h)
        client.explorer_path = "/tmp/" + "아주긴폴더이름" * 6
        chunk = client._explorer_chunk()
        self.assertEqual(self._display_width(chunk), (width + 1) * area_h)
        self.assertIn("Files: …".encode(), chunk)  # label survives a cut

    def test_explorer_and_sidebar_never_show_together(self):
        from crowcli.client import Client
        client = Client(object(), sidebar_data_provider=lambda: {
            "sessions": [], "hosts": [{"name": "mac", "address": "u@h"}]})
        self.assertNotEqual(client._sidebar_chunk(), b"")
        self.assertEqual(client._area()[0], 28)
        client.explorer_open = True
        self.assertEqual(client._sidebar_chunk(), b"")
        # the explorer takes the sidebar's column; panes sit beside it
        self.assertEqual(client._area()[0], client._sidebar_span()[0] + 1)
        client.explorer_open = False
        self.assertEqual(client._area()[0], 28)

    def test_pad_measures_display_width(self):
        from crowcli.client import Client
        client = Client(object())
        self.assertEqual(client._width(client._pad(" 리버스 에이전트", 38, "─")), 38)
        self.assertEqual(client._width(client._pad("대상: 로컬 샌드박스", 38)), 38)


class KoreanPrefixTests(unittest.TestCase):
    """두벌식 IME: prefix commands must work without switching to English."""

    def test_jamo_maps_to_prefix_keys(self):
        from crowcli.client import Client, latin_key
        self.assertEqual(latin_key("ㅇ"), "d")
        self.assertEqual(latin_key("ㄷ"), "e")
        self.assertEqual(latin_key("d"), "d")
        client = Client(object())
        client._prefix_pending = True
        result = client._input("ㅇ".encode(), {})
        self.assertEqual(result.action, "detach")
        client._prefix_pending = True
        result = client._input("ㅊ".encode(), {})
        self.assertEqual(result.action, "new")

    def test_prefix_arrow_repeats_like_tmux(self):
        from crowcli.client import Client
        moves, sent = [], []

        class Conn(object):
            def send(self, kind, data=b""):
                sent.append(data)

        class Active(object):
            conn = Conn()
            pane = {}
            meta = {}
        client = Client(object())
        client._focus_direction = lambda d, r: moves.append(d) or True
        client._active = lambda: Active()
        client._start_image_paste = lambda active: False
        client._input(b"\x02\x1b[C", {})
        client._input(b"\x1b[C", {})  # within repeat-time: no prefix
        client._input(b"\x1b[B", {})
        self.assertEqual(moves, ["right", "right", "down"])
        self.assertEqual(sent, [])
        client._input(b"x", {})  # any other key ends repeat mode
        client._input(b"\x1b[C", {})
        self.assertEqual(moves, ["right", "right", "down"])
        client._repeat_until = 0.0
        client._input(b"\x02\x1b[A", {})
        client._repeat_until = 1.0  # expired
        client._input(b"\x1b[A", {})
        self.assertEqual(moves[-1:], ["up"])
        self.assertEqual(len(moves), 4)

    def test_jamo_in_panels(self):
        from crowcli.client import Client
        client = Client(object())
        client._prefix_pending = True
        client._input("ㄷ".encode(), {})  # physical E opens the explorer
        self.assertTrue(client.explorer_open)
        client._explorer_key("ㅂ".encode())  # physical Q closes it
        self.assertFalse(client.explorer_open)

    def test_prefix_switches_ime_to_latin_and_back(self):
        from unittest import mock
        from crowcli import client as client_mod
        calls = []
        with mock.patch.object(client_mod.imswitch, "to_latin",
                               lambda: calls.append("latin") or "KO"), \
                mock.patch.object(client_mod.imswitch, "restore",
                                  lambda sid: calls.append(sid)):
            client = client_mod.Client(object())
            client._input(b"\x02", {})
            self.assertEqual(calls, ["latin"])
            client._input(b"z", {})  # command done: back to Korean
            self.assertEqual(calls, ["latin", "KO"])
            client._input(b"\x02", {})
            client._input(b"e", {})  # explorer stays in ABC while open
            self.assertEqual(calls, ["latin", "KO", "latin"])
            client._explorer_key(b"q")
            client._input(b"\x1b[A", {})
            self.assertEqual(calls[-1], "KO")

