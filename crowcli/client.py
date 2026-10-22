"""Attach renderer: composites every pane of a window onto the real
terminal (tmux/herdr style), with a status bar on the last row.

Each pane is a crow-session daemon; its output feeds a per-pane VT
screen (`crowcli/vt.py`) and this client paints the grids according to
the window's split layout. The child PTYs never see the real terminal
size — they get their pane rectangle — so full-screen agent TUIs stay
inside their pane and the borders/status bar are never overwritten.
"""
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty

from . import clipimage, daemon as daemon_mod, imswitch, \
    registry as registry_mod
from .vt import DEFAULT_ATTR, Screen, char_width, sgr_for

PREFIX = b"\x02"  # Ctrl-B, like tmux

HELP_KEYS = [
    ("d", "detach (세션 유지)"),
    ("%", "좌우로 분할"),
    ('"', "위아래로 분할"),
    ("←↑↓→ o", "pane 이동 (방향키는 0.5초 안에 연타 가능)"),
    ("z", "pane 확대 / 복원"),
    ("x", "pane 닫기"),
    ("c", "새 윈도우"),
    ("n  p", "다음 / 이전 윈도우"),
    ("0-9", "윈도우 선택"),
    ("s", "세션 선택"),
    (",  $", "윈도우 / 세션 이름"),
    ("e", "파일 탐색기"),
    ("a", "호스트 · 리버스"),
    ("C-b", "Ctrl-B 그대로 보내기"),
]

MOUSE_RE = re.compile(rb"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")
ARROW_RE = re.compile(rb"^\x1b[\[O][ABCD]$")
OSC_COLOUR_RE = re.compile(rb"\x1b\](1[01]);(rgb:[0-9a-fA-F/]+)(?:\x07|\x1b\\)")

KOREAN_TO_KEY = dict(pair.split(":") for pair in (
    "ㄱ:r ㄲ:r ㄴ:s ㄷ:e ㄸ:e ㄹ:f ㅁ:a ㅂ:q ㅃ:q ㅅ:t ㅆ:t ㅇ:d "
    "ㅈ:w ㅉ:w ㅊ:c ㅋ:z ㅌ:x ㅍ:v ㅎ:g ㅏ:k ㅐ:o ㅒ:o ㅑ:i ㅓ:j "
    "ㅔ:p ㅖ:p ㅕ:u ㅗ:h ㅛ:y ㅜ:n ㅠ:b ㅡ:m ㅣ:l").split())


def latin_key(key):
    """The QWERTY key a 두벌식 jamo came from (identity otherwise)."""
    return KOREAN_TO_KEY.get(key, key)


class AttachResult(object):
    def __init__(self, action, arg=None):
        self.action = action  # detach|switch|new|exit|choose|dead|split-*|kill-pane
        self.arg = arg


DIVIDER = 2  # cells between split panes (one edge line per pane)


def compute_rects(tree, panes_by_id, x, y, w, h, zoomed=None):
    """Layout tree + area -> {pane_id: (x, y, w, h)} (0-based)."""
    if zoomed and zoomed in panes_by_id:
        return {zoomed: (x, y, w, h)}
    if not tree or w <= 0 or h <= 0:
        return {}
    if "leaf" in tree:
        if tree["leaf"] in panes_by_id:
            return {tree["leaf"]: (x, y, w, h)}
        return {}
    ratio = tree.get("ratio", 0.5)
    # Dividers are two cells: each neighbour owns one edge line, so the
    # focused pane's own edge can be coloured without lighting up the
    # pane on the other side.
    gap = DIVIDER
    if tree["split"] == "v":  # side by side
        first = max(1, int((w - gap) * ratio)) if w > gap + 1 else w
        rects = compute_rects(tree["a"], panes_by_id, x, y, first, h)
        rects.update(compute_rects(tree["b"], panes_by_id,
                                    x + first + gap, y,
                                    max(1, w - first - gap), h))
        return rects
    first = max(1, int((h - gap) * ratio)) if h > gap + 1 else h
    rects = compute_rects(tree["a"], panes_by_id, x, y, w, first)
    rects.update(compute_rects(tree["b"], panes_by_id, x, y + first + gap,
                                w, max(1, h - first - gap)))
    return rects


class PaneRuntime(object):
    def __init__(self, pane, window):
        self.pane = pane
        self.window = window
        self.conn = None
        self.screen = Screen()
        self.awaiting_replay = False  # next non-reset "O" frame is replay
        self.dead_code = None
        self.view_offset = 0  # wheel scrollback view; 0 = live

    @property
    def id(self):
        return self.pane["id"]

    def root(self):
        meta = self.pane.get("meta", {})
        return meta.get("session_root") or \
            self.window.get("meta", {}).get("session_root")

    def connect(self):
        self.conn = daemon_mod.Daemon.connect(self.id, root=self.root())
        return self.conn is not None


class Client(object):
    def __init__(self, hooks, status_provider=None,
                 explorer_root_provider=None, reverse_data_provider=None,
                 sidebar_data_provider=None, notice_provider=None,
                 windows_provider=None, host_form=None, image_target=None,
                 key_actions=None):
        self.hooks = hooks
        # {"list": () -> [key dict], "copy": key -> message,
        #  "delete": (key, delete_files) -> message}
        self.key_actions = key_actions or {}
        self.key_list = []
        self._pending_key = None
        # (pane meta, window meta) -> uploader(png bytes) -> path, or None
        # to pass Ctrl-V through (local panes paste images themselves).
        self.image_target = image_target
        self._paste_busy = False
        self._paste_lock = threading.Lock()
        self._paste_queue = []
        self._paste_msg = None  # (text, shown until)
        # {"keys": () -> [(label, path)], "derive": address -> name,
        #  "save": (address, port_text, identity) -> host record,
        #  raising ValueError with a message to show}
        self.host_form = host_form
        self.form = None
        self.windows_provider = windows_provider
        self.notice_provider = notice_provider
        self.status_provider = status_provider
        self.explorer_root_provider = explorer_root_provider
        self.reverse_data_provider = reverse_data_provider
        self.sidebar_data_provider = sidebar_data_provider
        self._sidebar_data = {"sessions": [], "hosts": []}
        self._sidebar_at = 0.0
        self._sidebar_rows = []
        self._host_status = {}
        self._probes_running = set()
        self._probe_dirty = False
        self.reverse_open = False
        self.reverse_stage = "provider"
        self.reverse_data = {"hosts": [], "sessions": []}
        self.reverse_sel = 0
        self.reverse_scroll = 0
        self._pending_host = None
        self._pending_local = False
        self.explorer_open = False
        self.explorer_focus = False
        self.help_open = False
        self._help_opened_at = 0.0
        self._help_full = False
        self.explorer_path = None
        self.explorer_entries = []
        self.explorer_sel = 0
        self.explorer_scroll = 0
        self._bar_buttons = []
        self._last_click = (0.0, None)
        self._resize = False
        self._session_name = ""
        self._window = None
        self._last_draw = 0.0
        self.runtimes = []
        self.active_id = None
        self.last_active_pane_id = None
        self._outer_modes = None
        self._last_bar = 0.0
        self._prefix_pending = False
        self._repeat_until = 0.0  # tmux repeat-time after C-b arrow
        self._early_input = b""  # keys typed during the colour probe
        self._ime_saved = None
        self._stdin_buf = b""
        self._stdin_since = 0.0

    # -- terminal helpers ---------------------------------------------------
    def _winsize(self):
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except OSError:
            return 80, 24

    def _sidebar_span(self):
        """(content_width, divider_x, pane_x), all 0-based columns.
        The file explorer and the sidebar share the left column: while
        the explorer is open it takes the sidebar's place."""
        cols, _ = self._winsize()
        if self.explorer_open:  # docked beside the panes, never over them
            width = min(40, max(20, cols // 3))
            return width, width, width + 1
        if not Client.sidebar_open:
            return 0, 0, 0
        content = 27 if cols >= 80 else max(16, cols // 3)
        return content, content, content + 1

    def _area(self):
        cols, rows = self._winsize()
        _content, _divider, pane_x = self._sidebar_span()
        return pane_x, 0, max(1, cols - pane_x), \
            max(1, rows - 1)  # last row is the status bar

    def _rects(self):
        panes = dict((p["id"], p) for p in
                     registry_mod.window_panes(self._window))
        return compute_rects(self._window.get("layout"), panes,
                             *self._area(),
                             zoomed=self._window.get("zoomed"))

    def _runtime(self, pane_id):
        for runtime in self.runtimes:
            if runtime.id == pane_id:
                return runtime
        return None

    def _active(self):
        return self._runtime(self.active_id) or (
            self.runtimes[0] if self.runtimes else None)

    # -- attach ---------------------------------------------------------------
    def attach(self, session_name, window):
        """Block until detach / window exit / switch / split request."""
        self._session_name = session_name
        self._window = window
        panes = registry_mod.window_panes(window)
        if not panes:
            raise RuntimeError("window has no panes")
        active = registry_mod.active_pane(window)
        self.active_id = active["id"] if active else panes[0]["id"]
        self.runtimes = [PaneRuntime(p, window) for p in panes]
        missing = []
        for runtime in self.runtimes:
            if not runtime.connect():
                code = daemon_mod.has_exited(runtime.id,
                                             root=runtime.root())
                if code is not None:
                    return AttachResult("dead", (runtime.id, code))
                missing.append(runtime.pane["name"])
        if missing:
            raise RuntimeError("pane %s is not running" % missing[0])
        rects = self._rects()
        stdin_fd = sys.stdin.fileno()
        old = termios.tcgetattr(stdin_fd)
        self._resize = False
        old_handler = signal.signal(signal.SIGWINCH, self._on_resize)
        out = sys.stdout.buffer
        try:
            tty.setraw(stdin_fd)
            _, rows = self._winsize()
            # Reserve the last row: the outer terminal's scroll region is
            # everything above it, so nothing (scrolling, full-width
            # writes, app redraws) can ever roll over the status bar.
            out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1002h\x1b[?1006h"
                      + ("\x1b[1;%dr" % max(1, rows - 1)).encode())
            out.flush()
            self._probe_outer_colors(out, stdin_fd)
            for runtime in self.runtimes:
                rect = rects.get(runtime.id)
                if rect:
                    runtime.screen.resize(rect[2], rect[3])
                runtime.awaiting_replay = True
                runtime.conn.send("A")
                if rect:
                    runtime.conn.send_size(rect[2], rect[3])
                runtime.conn.sock.setblocking(False)
            result = self._loop(out, stdin_fd)
            self.last_active_pane_id = self.active_id
            return result
        finally:
            self._restore_ime()
            self.last_active_pane_id = self.active_id
            for runtime in self.runtimes:
                if runtime.conn:
                    runtime.conn.close()
            out.write(b"\x1b[r"
                      b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
                      b"\x1b[?2004l\x1b[?1l\x1b[?25h\x1b[?1049l")
            out.flush()
            signal.signal(signal.SIGWINCH, old_handler)
            termios.tcsetattr(stdin_fd, termios.TCSANOW, old)

    _colors_probed = False  # class-wide: once per crow process

    def _probe_outer_colors(self, out, stdin_fd):
        """Ask the real terminal for its default fg/bg (OSC 10/11) so
        panes asking the same get the truth, not a dark-theme guess."""
        if Client._colors_probed:
            return
        Client._colors_probed = True
        out.write(b"\x1b]10;?\x07\x1b]11;?\x07")
        out.flush()
        buf, deadline, colours = b"", time.time() + 0.3, {}
        while len(colours) < 2 and time.time() < deadline:
            if not select.select([stdin_fd], [], [],
                                 max(0, deadline - time.time()))[0]:
                break
            buf += os.read(stdin_fd, 4096)
            for match in OSC_COLOUR_RE.finditer(buf):
                colours[match.group(1).decode()] = match.group(2).decode()
        Screen.term_colors = colours
        self._early_input = OSC_COLOUR_RE.sub(b"", buf)

    def _on_resize(self, _signo, _frame):
        self._resize = True

    # -- layout / focus ---------------------------------------------------------
    def _apply_sizes(self, rects):
        for runtime in self.runtimes:
            rect = rects.get(runtime.id)
            if not rect:
                continue
            if (runtime.screen.cols, runtime.screen.rows) != (rect[2],
                                                              rect[3]):
                runtime.screen.resize(rect[2], rect[3])
                try:
                    runtime.conn.send_size(rect[2], rect[3])
                except socket.error:
                    pass

    def _focus(self, pane_id):
        if pane_id != self.active_id and self._runtime(pane_id):
            self.active_id = pane_id
            return True
        return False

    def _focus_direction(self, direction, rects):
        active = rects.get(self.active_id)
        if not active:
            return False
        ax, ay, aw, ah = active
        best, best_score = None, None
        for pane_id, (x, y, w, h) in rects.items():
            if pane_id == self.active_id:
                continue
            if direction == "left" and x + w <= ax:
                gap, overlap = ax - (x + w), min(ay + ah, y + h) - max(ay, y)
            elif direction == "right" and x >= ax + aw:
                gap, overlap = x - (ax + aw), min(ay + ah, y + h) - max(ay, y)
            elif direction == "up" and y + h <= ay:
                gap, overlap = ay - (y + h), min(ax + aw, x + w) - max(ax, x)
            elif direction == "down" and y >= ay + ah:
                gap, overlap = y - (ay + ah), min(ax + aw, x + w) - max(ax, x)
            else:
                continue
            score = (gap, -max(overlap, -999))
            if best_score is None or score < best_score:
                best, best_score = pane_id, score
        return self._focus(best) if best else False

    def _focus_cycle(self, rects):
        ids = [p for p in rects if self._runtime(p)]
        ids.sort(key=lambda p: (rects[p][1], rects[p][0]))
        if self.active_id in ids and len(ids) > 1:
            return self._focus(ids[(ids.index(self.active_id) + 1)
                                   % len(ids)])
        return False

    # -- sidebar (always visible) ---------------------------------------------
    @staticmethod
    def _fit(text, width):
        out, used = "", 0
        for char in text:
            w = char_width(char)
            if used + w > width:
                break
            out += char
            used += w
        return out + " " * max(0, width - used), used

    @staticmethod
    def _width(text):
        return sum(char_width(char) for char in text)

    def _pad(self, text, width, fill=" "):
        """Truncate + pad by *display* width (len() lies for Korean)."""
        out, used = "", 0
        for char in text:
            w = char_width(char)
            if used + w > width:
                break
            out += char
            used += w
        return out + fill * (width - used)

    @staticmethod
    def classify_probe_error(stderr):
        text = (stderr or "").lower()
        if "permission denied" in text:
            return "연결 실패 (인증 오류)"
        if "host key verification" in text:
            return "연결 실패 (호스트 키)"
        if "timed out" in text or "timeout" in text:
            return "연결 실패 (시간 초과)"
        if "could not resolve" in text or "name or service" in text \
                or "nodename" in text:
            return "연결 실패 (주소 확인 불가)"
        if "connection refused" in text:
            return "연결 실패 (연결 거부)"
        if "no route" in text or "unreachable" in text:
            return "연결 실패 (도달 불가)"
        return "연결 실패"

    def _refresh_sidebar(self):
        if not self.sidebar_data_provider:
            return
        now = time.time()
        if now - self._sidebar_at < 1.0:
            return
        self._sidebar_at = now
        try:
            self._sidebar_data = self.sidebar_data_provider() \
                or {"sessions": [], "hosts": []}
        except Exception:
            pass

    def _host_state(self, host):
        address = host.get("address") or host.get("name") or ""
        state = self._host_status.get(address)
        now = time.time()
        if state is None or (now - state["at"] > 30
                             and address not in self._probes_running):
            self._start_probe(host)
            if state is None:
                return "확인 중", "33"
        age = now - state["at"]
        if address in self._probes_running and state["state"] == "…":
            return "확인 중", "33"
        return state["state"], state["color"]

    def _start_probe(self, host):
        address = host.get("address") or host.get("name") or ""
        if not address or address in self._probes_running:
            return
        self._probes_running.add(address)
        previous = self._host_status.get(address)
        if previous is None:
            self._host_status[address] = {"state": "…", "color": "33",
                                          "at": time.time()}

        def probe():
            args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3"]
            if host.get("port"):
                args += ["-p", str(host["port"])]
            if host.get("identity"):
                args += ["-i", os.path.expanduser(host["identity"])]
            args += [address, "true"]
            try:
                done = subprocess.run(args, capture_output=True, text=True,
                                      timeout=8, stdin=subprocess.DEVNULL)
                code, stderr = done.returncode, done.stderr or ""
            except Exception as error:  # ssh missing, timeout, ...
                code, stderr = 1, str(error)
            self._host_status[address] = {
                "state": "연결됨" if code == 0
                         else self.classify_probe_error(stderr),
                "color": "32" if code == 0 else "31", "at": time.time()}
            self._probes_running.discard(address)
            self._probe_dirty = True

        threading.Thread(target=probe, daemon=True).start()

    # Class-wide: survives the new Client run_attach builds on every
    # window / session switch.
    sidebar_open = True

    def _toggle_sidebar(self):
        if self.explorer_open:  # the two share the left side
            self.explorer_open = False
            Client.sidebar_open = True
        else:
            Client.sidebar_open = not Client.sidebar_open
        self._focus_dirty = True

    def _sidebar_chunk(self):
        if self.explorer_open or not Client.sidebar_open:
            self._sidebar_rows = []
            return b""
        self._refresh_sidebar()
        content, divider, _pane_x = self._sidebar_span()
        _x, _y, _w, area_h = self._area()
        data = self._sidebar_data
        fail_style = "1;38;5;255;48;5;160"  # white on red: unmissable
        bg = "48;5;231"
        # basic ANSI colours are tuned for dark backgrounds; these stay
        # readable on the white sidebar
        tone = {"32": "38;5;28", "31": "38;5;160", "33": "38;5;130",
                "37": "38;5;242"}
        plain = "38;5;238;" + bg
        heading = "1;38;5;31;" + bg
        lines = []  # (text, style, action)
        notice = None
        if self.notice_provider:
            try:
                notice = self.notice_provider()
            except Exception:
                notice = None
        if notice:
            lines.append((" ✗ 실패", fail_style, None))
            text, chunk, used = str(notice), "", 0
            for char in text:  # wrap by display width, not char count
                w = char_width(char)
                if used + w > content - 1 and chunk:
                    lines.append((" " + chunk, fail_style, None))
                    chunk, used = "", 0
                chunk += char
                used += w
            if chunk:
                lines.append((" " + chunk, fail_style, None))
        lines.append(("", plain, None))
        lines.append((" 세션", heading, None))
        status_color = {"연결됨": "32", "실행 중": "32", "브리지 끊김": "33",
                        "재접속 대기": "33", "대기": "33"}
        for session in data.get("sessions", []):
            marker = "▸" if session.get("current") else " "
            label = "%s %s" % (marker, session["name"])
            action = None if session.get("current") else \
                ("switch-session", session["name"])
            lines.append((label, "1;38;5;16;" + bg if session.get("current")
                           else plain, action))
            status = session.get("status", "")
            if status:
                failed = "실패" in status or status in ("종료", "브리지 끊김")
                style = fail_style if failed else \
                    tone[status_color.get(status, "37")] + ";" + bg
                lines.append(("   ✗ " + status if failed
                              else "   ● " + status, style, action))
        lines.append(("", plain, None))
        lines.append((" 호스트 · 리버스", heading, None))
        for host in data.get("hosts", []):
            state, color = self._host_state(host)
            failed = state.startswith("연결 실패")
            lines.append((" ✗ " + host.get("name", "?") if failed
                          else " ● " + host.get("name", "?"),
                          fail_style if failed else tone.get(color, color) + ";" + bg,
                          ("reverse-host", host)))
            lines.append(("   " + state,
                          fail_style if failed else tone.get(color, color) + ";" + bg,
                          ("reverse-host", host)))
        if not data.get("hosts"):
            lines.append((" (없음 — 아래에서 추가)",
                          "38;5;245;" + bg, None))
        lines.append((" + 호스트 추가…", "38;5;31;" + bg,
                      ("add-host", None)))
        lines.append(("", plain, None))
        lines.append((" SSH 키", heading, None))
        keys = data.get("keys", [])
        for key in keys[:5]:
            lines.append((" ◆ " + key["name"], plain, ("key", key)))
        if len(keys) > 5:
            lines.append(("   … 외 %d개" % (len(keys) - 5),
                          "38;5;245;" + bg, ("keys", None)))
        lines.append((" + 새 키 만들기…", "38;5;31;" + bg,
                      ("key-generate", None)))
        lines.append((" ⋯ 키 목록·관리", "38;5;31;" + bg, ("keys", None)))
        out = []
        self._sidebar_rows = []
        for row in range(area_h):
            text, style, action = lines[row] if row < len(lines) \
                else ("", plain, None)
            fitted, _used = self._fit(text, content)
            out.append(("\x1b[%d;1H\x1b[%sm" % (row + 1, style)).encode()
                       + fitted.encode()
                       + b"\x1b[38;5;250m" + "│".encode() + b"\x1b[0m")
            if action:
                self._sidebar_rows.append((row, action))
        return b"".join(out)

    def _sidebar_click(self, gy):
        for row, (kind, arg) in self._sidebar_rows:
            if row == gy - 1:
                if kind == "switch-session":
                    return AttachResult("switch-session", arg)
                if kind == "reverse-host":
                    self._open_reverse_for_host(arg)
                elif kind == "add-host":
                    self._open_host_form()
                elif kind in ("key", "keys"):
                    self._open_keys(arg)
                elif kind == "key-generate":
                    return AttachResult("key-generate", None)
        return None

    def _open_keys(self, key=None):
        """Key manager in the right panel: list → one key's actions."""
        self.explorer_open = False
        self.help_open = False
        self._load_keys()
        self._pending_key = None
        self.reverse_stage = "keys"
        if key:
            found = next((k for k in self.key_list
                          if k["path"] == key.get("path")), None)
            if found:
                self._pending_key = found
                self.reverse_stage = "key"
        self.reverse_sel = 0
        self.reverse_scroll = 0
        self.reverse_open = True
        self._focus_dirty = True

    def _load_keys(self):
        try:
            self.key_list = self.key_actions["list"]() \
                if "list" in self.key_actions else []
        except Exception:
            self.key_list = []

    def _open_reverse_for_host(self, host):
        self._toggle_reverse()  # opens (loads fresh panel data)
        if not self.reverse_open:
            self._toggle_reverse()
        self._pending_host = host
        self._pending_local = False
        self.reverse_stage = "menu"
        self.reverse_sel = 0
        self._focus_dirty = True

    # -- status bar -----------------------------------------------------------
    FILES_LABEL = " Files "
    HOSTS_LABEL = " Hosts "
    BUTTON_STYLE = b"\x1b[0;1;48;5;32;38;5;255m"
    BUTTON_ON_STYLE = b"\x1b[0;1;48;5;31;38;5;231m"

    BAR_STYLE = b"\x1b[0;48;5;24;38;5;255m"
    TAB_STYLE = b"\x1b[0;48;5;24;38;5;250m"
    TAB_CURRENT_STYLE = b"\x1b[0;1;48;5;45;38;5;16m"
    SESSION_STYLE = b"\x1b[0;48;5;238;38;5;255m"

    def _bar_text(self, width=None):
        """Left (session) and right (clock / hints) halves of the bar."""
        if self.status_provider:
            left = self.status_provider()
        else:
            left = " crow-cli | %s " % self._session_name
        if self._paste_msg and time.time() < self._paste_msg[1]:
            left = " %s " % self._paste_msg[0]
        right = " %s | C-b d detach | C-b ? help " % time.strftime("%H:%M")
        cols = width if width is not None else self._winsize()[0]
        if self._width(left) + self._width(right) > cols:
            # Shrink the hints before truncating session/window names.
            right = " %s " % time.strftime("%H:%M")
        return left, right

    def _window_tabs(self):
        if not self.windows_provider:
            return []
        return [(" %d:%s " % (i, name), current)
                for i, (name, current) in enumerate(self.windows_provider())]

    # -- rendering -------------------------------------------------------------
    def _render(self, out, rects, force=False):
        active = self._active()
        chunks = []
        for runtime in self.runtimes:
            rect = rects.get(runtime.id)
            if not rect:
                continue
            x, y, w, h = rect
            scrolled = runtime.view_offset > 0
            rows = range(h) if (force or scrolled) else \
                sorted(runtime.screen.dirty)
            visible = runtime.screen.lines_for_view(runtime.view_offset)
            for row in rows:
                if row >= h or row >= len(visible):
                    continue
                chunks.append(("\x1b[%d;%dH" % (y + row + 1, x + 1)).encode())
                attr = DEFAULT_ATTR
                line = visible[row]
                for cell in line:
                    if cell[0] == "":  # wide-char continuation
                        continue
                    if cell[1] != attr:
                        chunks.append(sgr_for(cell[1], attr))
                        attr = cell[1]
                    chunks.append(cell[0].encode(errors="replace"))
                if attr != DEFAULT_ATTR:
                    chunks.append(b"\x1b[0m")
            runtime.screen.mark_clean()
        chunks.append(self._sidebar_chunk())
        chunks.append(self._borders(rects))
        if self.explorer_open:
            chunks.append(self._explorer_chunk())
        if self.reverse_open:
            chunks.append(self._reverse_chunk())
        chunks.append(self._bar_chunk())
        if self.help_open:
            chunks.append(self._help_chunk())
        if self.form is not None:
            chunks.append(self._form_chunk())
        chunks.append(self._cursor_chunk(rects))
        out.write(b"".join(chunks))
        out.flush()
        self._last_draw = time.time()
        self._last_bar = self._last_draw

    def _bar_chunk(self):
        """Status bar bytes. No save/restore-cursor and no erase-line:
        the text is padded to full width and the scroll region already
        guarantees the row, so repainting is idempotent and flicker-free.
        Cursor placement is the caller's job (_cursor_chunk)."""
        cols, rows = self._winsize()
        hosts_open = Client.sidebar_open and not self.explorer_open
        hosts = " Hosts ▾" if hosts_open else self.HOSTS_LABEL
        files = " Files ▾" if self.explorer_open else self.FILES_LABEL
        hosts_w = self._width(hosts)
        files_w = hosts_w + self._width(files)
        width = max(0, cols - files_w)
        left, right = self._bar_text(width)
        if self._width(left) > cols - files_w:
            left = self._pad(left, max(0, cols - files_w))
        left_w, right_w = self._width(left), self._width(right)
        # Session block (own shade) | tabs flush left after a gap | hints
        # flush right.
        buttons = [(1, 1 + hosts_w, "sidebar"),
                   (1 + hosts_w, 1 + files_w, "explorer")]
        body = [self.SESSION_STYLE, left.encode(errors="replace"),
                self.BAR_STYLE]
        col = files_w + left_w
        if col + 1 <= cols:
            body.append(b" ")
            col += 1
        hi = cols - right_w if col + right_w <= cols else cols
        for index, (label, current) in enumerate(self._window_tabs()):
            label_w = self._width(label)
            if col + label_w > hi:
                break
            body.append(self.TAB_CURRENT_STYLE if current else self.TAB_STYLE)
            body.append(label.encode(errors="replace"))
            buttons.append((col + 1, col + 1 + label_w, ("window", index)))
            col += label_w
        body.append(self.BAR_STYLE)
        if col + right_w <= cols:
            body.append(b" " * (cols - col - right_w)
                        + right.encode(errors="replace"))
        else:
            body.append(b" " * max(0, cols - col))
        self._bar_buttons = buttons
        def button(label, on):
            style = self.BUTTON_ON_STYLE if on else self.BUTTON_STYLE
            return style + label.encode()
        return (("\x1b[%d;1H" % rows).encode()
                + button(hosts, hosts_open)
                + button(files, self.explorer_open)
                + b"".join(body)
                + b"\x1b[0m")

    def _cursor_chunk(self, rects):
        """Outer modes + real cursor position follow the active pane."""
        active = self._active()
        if active is None:
            return b""
        chunks = []
        rect = rects.get(active.id)
        screen = active.screen
        modes = (screen.app_cursor, screen.bracketed_paste)
        if modes != self._outer_modes:
            self._outer_modes = modes
            chunks.append(b"\x1b[?1" + (b"h" if modes[0] else b"l"))
            chunks.append(b"\x1b[?2004" + (b"h" if modes[1] else b"l"))
        if self.form is not None and self.form.get("cursor"):
            row, col = self.form["cursor"]
            chunks.append(("\x1b[%d;%dH\x1b[?25h" % (row, col)).encode())
        elif (self.explorer_open and self.explorer_focus) or \
                self.reverse_open or self.help_open or self.form is not None:
            chunks.append(b"\x1b[?25l")
        elif rect and screen.cursor_visible and \
                active.view_offset == 0:
            chunks.append(("\x1b[%d;%dH\x1b[?25h"
                           % (rect[1] + screen.cy + 1,
                              rect[0] + screen.cx + 1)).encode())
        else:
            chunks.append(b"\x1b[?25l")
        return b"".join(chunks)

    def _repaint_bar(self, out, rects):
        out.write(self._bar_chunk() + self._cursor_chunk(rects))
        out.flush()
        self._last_bar = time.time()

    def _borders(self, rects):
        """Each pane draws its own thin edge line on the side(s) facing a
        neighbour; only the focused pane's lines are coloured. Lines live
        in the divider cells, never on top of pane content."""
        if len(rects) < 2:
            return b""
        area_x, area_y, area_w, area_h = self._area()
        out = []
        for pane_id, (x, y, w, h) in rects.items():
            style = b"\x1b[1;38;5;51m" if pane_id == self.active_id \
                else b"\x1b[38;5;240m"
            left, right = x > area_x, x + w < area_x + area_w
            top, bottom = y > area_y, y + h < area_y + area_h
            # Box-drawing glyphs only: Terminal.app draws block elements
            # (▕ ▔ …) shorter than a row, so they come out dashed. Every line
            # sits mid-cell, so each pane's corners close cleanly.
            cells = []
            if left:
                cells += [(x - 1, row, "│") for row in range(y, y + h)]
            if right:
                cells += [(x + w, row, "│") for row in range(y, y + h)]
            if top:
                cells += [(col, y - 1, "─") for col in range(x, x + w)]
            if bottom:
                cells += [(col, y + h, "─") for col in range(x, x + w)]
            for has, cx, cy, char in ((left and top, x - 1, y - 1, "┌"),
                                      (right and top, x + w, y - 1, "┐"),
                                      (left and bottom, x - 1, y + h, "└"),
                                      (right and bottom, x + w, y + h, "┘")):
                if has:
                    cells.append((cx, cy, char))
            for cx, cy, char in cells:
                out.append(("\x1b[%d;%dH" % (cy + 1, cx + 1)).encode()
                           + style + char.encode() + b"\x1b[0m")
        return b"".join(out)

    # -- main loop ---------------------------------------------------------------
    def _loop(self, out, stdin_fd):
        rects = self._rects()
        self._render(out, rects, force=True)
        sockets = dict((r.conn.sock, r) for r in self.runtimes)
        if self._early_input:
            early, self._early_input = self._early_input, b""
            result = self._input(early, rects)
            if result is not None:
                return result
        paint_due = False  # coalesce pane output into ~60fps paints
        while True:
            if self._resize:
                self._resize = False
                rects = self._rects()
                self._apply_sizes(rects)
                _, rows = self._winsize()
                out.write(("\x1b[1;%dr" % max(1, rows - 1)).encode())
                out.flush()
                self._render(out, rects, force=True)
            if self._help_animating() or self._form_animating():
                paint_due = True
            if self._paste_queue:
                self._flush_pastes()
            watch = list(sockets) + [stdin_fd]
            # While a paint is pending, poll fast so a burst of small
            # socket chunks (agent TUIs redraw in many writes, worse
            # over forwarded ssh) lands in ONE paint instead of showing
            # every torn intermediate frame.
            ready, _, _ = select.select(
                watch, [], [], 0.004 if paint_due else 0.05)
            changed = False
            for sock in [s for s in ready if s != stdin_fd]:
                runtime = sockets[sock]
                try:
                    data = sock.recv(65536)
                except socket.error:
                    data = b""
                if not data:
                    return AttachResult(
                        "dead", (runtime.id,
                                 daemon_mod.has_exited(
                                     runtime.id, root=runtime.root())))
                runtime.conn.feed(data)
                for kind, payload in runtime.conn.frames():
                    if kind == "O":
                        if runtime.awaiting_replay and payload != b"\x1bc":
                            runtime.awaiting_replay = False
                            runtime.screen.feed_replay(payload)
                            # queries in old output were answered back then
                            runtime.screen.responses = []
                        else:
                            runtime.screen.feed(payload)
                        changed = True
                        for response in runtime.screen.responses:
                            try:
                                runtime.conn.send("D", response)
                            except socket.error:
                                pass
                        runtime.screen.responses = []
                    elif kind == "X":
                        code = struct.unpack(">I", payload[:4])[0] \
                            if len(payload) >= 4 else 0
                        return AttachResult("dead", (runtime.id, code))
            if changed:
                paint_due = True
            if paint_due and time.time() - self._last_draw >= 0.016:
                self._render(out, rects)
                paint_due = False
            if stdin_fd in ready:
                data = os.read(stdin_fd, 65536)
                if not data:
                    return AttachResult("detach")
                result = self._input(data, rects)
                if result is not None:
                    return result
                if self._focus_dirty:
                    new_rects = self._rects()
                    if new_rects != rects:
                        rects = new_rects
                        self._apply_sizes(rects)
                    self._render(out, rects, force=True)
                    paint_due = False
            if paint_due and time.time() - self._last_draw >= 0.1:
                # A pane went quiet mid-burst: don't leave a stale frame.
                self._render(out, rects)
                paint_due = False
            if not changed and not paint_due and (self._probe_dirty
                                or time.time() - self._last_bar >= 0.5):
                # Sidebar connection states + bar clock stay fresh even
                # between output bursts (scroll region keeps the bar
                # itself from ever being overwritten).
                self._probe_dirty = False
                self._render(out, rects)
            elif self._stdin_buf == b"\x1b" and \
                    time.time() - self._stdin_since > 0.05:
                # A lone ESC key press, not the start of a sequence.
                result = self._input(b"", rects, flush_esc=True)
                if result is not None:
                    return result

    _focus_dirty = False

    # -- input ---------------------------------------------------------------------
    def _input(self, data, rects, flush_esc=False):
        self._focus_dirty = False
        self._stdin_buf += data
        if flush_esc:
            units, self._stdin_buf = [b"\x1b"], b""
        else:
            units = []
            while self._stdin_buf:
                unit = self._parse_unit(self._stdin_buf)
                if unit is None:
                    break
                units.append(unit)
                self._stdin_buf = self._stdin_buf[len(unit):]
            if self._stdin_buf:
                self._stdin_since = time.time()
        for unit in units:
            result = self._unit(unit, rects)
            if result is not None:
                return result
        return None

    @staticmethod
    def _parse_unit(buf):
        """One key unit: a byte, or a whole escape sequence."""
        if not buf:
            return None
        if buf[0:1] != b"\x1b":
            lead = buf[0]
            if lead < 0x80:
                return buf[:1]
            size = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 \
                else 2 if lead >= 0xC0 else 1
            return buf[:size] if len(buf) >= size else None
        if len(buf) == 1:
            return None
        if buf[1:2] == b"[":
            for i in range(2, len(buf)):
                if 0x40 <= buf[i] <= 0x7E:
                    return buf[:i + 1]
            return None
        if buf[1:2] == b"O":
            return buf[:3] if len(buf) >= 3 else None
        if buf[1:2] == b"]":
            end = buf.find(b"\x07", 2)
            return buf[:end + 1] if end >= 0 else None
        return buf[:2]

    def _restore_ime(self):
        saved, self._ime_saved = self._ime_saved, None
        imswitch.restore(saved)

    def _unit(self, unit, rects):
        result = self._unit_inner(unit, rects)
        # A Korean IME would hold the command key in its composition
        # buffer, so prefix mode (and panels opened from it) runs in ABC.
        if self._ime_saved and not (
                self._prefix_pending or self.reverse_open
                or (self.explorer_open and self.explorer_focus)):
            self._restore_ime()
        return result

    def _unit_inner(self, unit, rects):
        if self.form is not None:
            return self._form_key(unit)
        if self.help_open and not self._prefix_pending:
            mouse = MOUSE_RE.match(unit)
            if mouse and (mouse.group(4) == b"m"
                          or int(mouse.group(1)) & 32):
                return None  # releases / motion don't dismiss
            self.help_open = False
            self._focus_dirty = True
            return None
        if self._prefix_pending:
            self._prefix_pending = False
            return self._prefix_unit(unit, rects)
        if self._repeat_until:
            if time.time() < self._repeat_until and ARROW_RE.match(unit):
                return self._prefix_unit(unit, rects)
            self._repeat_until = 0.0
        if unit == PREFIX:
            self._prefix_pending = True
            if self._ime_saved is None:
                self._ime_saved = imswitch.to_latin()
            return None
        mouse = MOUSE_RE.match(unit)
        if mouse:
            self._mouse(unit, mouse, rects)
            result, self._explorer_result = self._explorer_result, None
            return result
        if self.reverse_open:
            return self._reverse_key(unit)
        if self.explorer_open and self.explorer_focus:
            return self._explorer_key(unit)
        active = self._active()
        if unit == b"\x16" and self._start_image_paste(active):
            return None
        if active is not None and active.conn is not None:
            active.view_offset = 0
            try:
                active.conn.send("D", unit)
            except socket.error:
                return AttachResult("detach")
        return None

    # -- clipboard image paste (Ctrl-V) -----------------------------------------
    def _start_image_paste(self, runtime):
        if runtime is None or not self.image_target:
            return False
        uploader = self.image_target(runtime.pane.get("meta") or {},
                                     runtime.window.get("meta") or {})
        if uploader is None:
            return False
        if self._paste_busy:
            self._flash("이미지 붙여넣기 진행 중…")
            return True
        self._paste_busy = True

        def work():
            try:
                data = clipimage.read_png()
                if data is None:  # no image: an ordinary Ctrl-V
                    self._paste_done(runtime, b"\x16", None)
                    return
                self._flash("클립보드 이미지 올리는 중… (%d KB)"
                            % (len(data) // 1024 + 1), 60)
                path = uploader(data)
                self._paste_done(runtime,
                                 clipimage.pasted_path(path).encode(),
                                 "이미지 첨부: " + path)
            except Exception as error:
                self._paste_done(runtime, None,
                                 "이미지 붙여넣기 실패: %s" % error)

        threading.Thread(target=work, daemon=True).start()
        return True

    def _flash(self, text, seconds=4):
        self._paste_msg = (text, time.time() + seconds)
        self._probe_dirty = True

    def _paste_done(self, runtime, data, message):
        with self._paste_lock:
            self._paste_queue.append((runtime, data, message))

    def _flush_pastes(self):
        with self._paste_lock:
            done, self._paste_queue = self._paste_queue, []
        for runtime, data, message in done:
            self._paste_busy = False
            if message:
                self._flash(message)
            if data and runtime in self.runtimes and runtime.conn:
                runtime.view_offset = 0
                try:
                    runtime.conn.send("D", data)
                except socket.error:
                    pass

    def _mouse(self, unit, match, rects):
        button, gx, gy = int(match.group(1)), int(match.group(2)), \
            int(match.group(3))
        final = match.group(4)
        _, rows = self._winsize()
        if gy >= rows:  # status bar
            if final == b"M":
                for x0, x1, action in self._bar_buttons:
                    if not x0 <= gx < x1:
                        continue
                    if action == "explorer":
                        self._toggle_explorer()
                    elif action == "sidebar":
                        self._toggle_sidebar()
                    elif action[0] == "window":
                        self._explorer_result = AttachResult(
                            "switch", ("index", action[1]))
            return
        pane_x = self._area()[0]
        if self.explorer_open and gx <= pane_x:
            if final == b"M" and not self.explorer_focus:
                self.explorer_focus = True
                self._focus_dirty = True
            self._explorer_click(button, gx, gy, final)
            return
        if gx <= pane_x:
            if final == b"M" and button not in (64, 65):
                result = self._sidebar_click(gy)
                if result is not None:
                    self._explorer_result = result
                else:
                    self._focus_dirty = True
            return
        if self.reverse_open:
            self._reverse_click(button, gx, gy, final)
            return
        if self.explorer_open and self.explorer_focus and final == b"M" \
                and button not in (64, 65):
            self.explorer_focus = False  # keys go to the clicked pane
            self._focus_dirty = True
        for pane_id, (x, y, w, h) in rects.items():
            if not (x < gx <= x + w and y < gy <= y + h):
                continue
            runtime = self._runtime(pane_id)
            if runtime is None:
                return
            if button in (64, 65):  # wheel
                if runtime.screen.mouse_mode:
                    self._forward_mouse(runtime, button, gx - x, gy - y,
                                        final)
                else:
                    limit = len(runtime.screen.scrollback)
                    if button == 64:
                        runtime.view_offset = min(limit,
                                                  runtime.view_offset + 3)
                    else:
                        runtime.view_offset = max(
                            0, runtime.view_offset - 3)
                    self._focus(pane_id)
                    self._focus_dirty = True
                return
            if final == b"M":
                self._focus_dirty = self._focus(pane_id) or \
                    self._focus_dirty
            if runtime.screen.mouse_mode:
                self._forward_mouse(runtime, button, gx - x, gy - y, final)
            return

    def _forward_mouse(self, runtime, button, lx, ly, final):
        try:
            runtime.conn.send("D", ("\x1b[<%d;%d;%d" % (button, lx, ly))
                              .encode() + final)
        except socket.error:
            pass

    # -- reverse agent panel --------------------------------------------------
    def _toggle_reverse(self):
        self.explorer_open = False
        if self.reverse_open:
            self.reverse_open = False
        else:
            if self.reverse_data_provider:
                try:
                    self.reverse_data = self.reverse_data_provider() \
                        or {"hosts": [], "sessions": []}
                except Exception:
                    self.reverse_data = {"hosts": [], "sessions": []}
            self.reverse_stage = "provider"
            self.reverse_sel = 0
            self.reverse_scroll = 0
            if not self._pending_host and not self._pending_local:
                hosts = self.reverse_data.get("hosts", [])
                if hosts:  # most recently used first (cli sorts)
                    self._pending_host = hosts[0]
            self.reverse_open = True
        self._focus_dirty = True

    def _reverse_targets(self):
        return [("host", host)
                for host in self.reverse_data.get("hosts", [])]

    def _cycle_target(self):
        targets = self._reverse_targets()
        if not targets:
            return
        current = ("local", None) if self._pending_local else \
            ("host", self._pending_host)
        index = next((i for i, t in enumerate(targets)
                      if t[0] == current[0]
                      and (t[0] == "local" or t[1] is current[1]
                           or (t[1] or {}).get("name")
                           == (current[1] or {}).get("name"))), -1)
        kind, host = targets[(index + 1) % len(targets)]
        self._pending_local = kind == "local"
        self._pending_host = host
        self.reverse_sel = 0

    KEY_STAGES = ("keys", "key", "key-host", "key-delete")

    def _key_rows(self):
        key = self._pending_key or {}
        if self.reverse_stage == "keys":
            rows = [{"kind": "key-generate",
                     "label": "+ 새 키 만들기 (Ed25519)"}]
            for managed, heading in ((True, "관리 키"),
                                     (False, "이 컴퓨터 · ~/.ssh")):
                group = [k for k in self.key_list
                         if bool(k.get("managed")) == managed]
                if not group:
                    continue
                rows.append({"kind": "info", "label": ""})
                rows.append({"kind": "info", "label": heading})
                for item in group:
                    fingerprint = item.get("sha256", "")
                    rows.append({"kind": "key-open", "key": item, "label":
                                 "◆ %s · %s %s" % (
                                     item["name"], item.get("type", "?"),
                                     fingerprint[7:15] + "…"
                                     if fingerprint else "")})
            return rows
        if self.reverse_stage == "key":
            rows = [{"kind": "key-copy", "label": "공개키 클립보드에 복사"},
                    {"kind": "key-install", "label": "서버에 공개키 설치 →"}]
            if key.get("managed"):
                rows += [{"kind": "key-rename", "label": "이름 변경…"},
                         {"kind": "key-remove", "label": "삭제…"}]
            rows += [{"kind": "info", "label": ""},
                     {"kind": "info", "label": "종류  %s · %s" % (
                         key.get("type", "?"), key.get("origin", ""))},
                     {"kind": "info", "label": "지문  " +
                      key.get("sha256", "?")},
                     {"kind": "info", "label": "경로  " + key.get(
                         "path", "").replace(os.path.expanduser("~"), "~", 1)},
                     {"kind": "info", "label": "사용  " + (
                         ", ".join(key.get("hosts") or []) or "없음")}]
            return rows
        if self.reverse_stage == "key-host":
            hosts = self.reverse_data.get("hosts") or \
                self._sidebar_data.get("hosts", [])
            if not hosts:
                return [{"kind": "info",
                         "label": "저장된 호스트가 없습니다"}]
            return [{"kind": "key-host-pick", "host": host,
                     "label": "%s — %s" % (host.get("name", "?"),
                                           host.get("address", ""))}
                    for host in hosts]
        rows = [{"kind": "key-unregister",
                 "label": "등록만 해제 (파일은 그대로)"}]
        if not key.get("imported"):
            rows.append({"kind": "key-delete-files",
                         "label": "개인키·공개키 파일까지 삭제"})
        rows.append({"kind": "key-back", "label": "취소"})
        return rows

    def _key_activate(self, row):
        kind, key = row["kind"], self._pending_key
        if kind == "key-open":
            self._pending_key = row["key"]
            self.reverse_stage = "key"
        elif kind == "key-generate":
            self.reverse_open = False
            return AttachResult("key-generate", None)
        elif kind == "key-copy" and "copy" in self.key_actions:
            self._flash(self.key_actions["copy"](key))
        elif kind == "key-install":
            self.reverse_stage = "key-host"
        elif kind == "key-host-pick":
            self.reverse_open = False
            return AttachResult("key-install", (key, row["host"]))
        elif kind == "key-rename":
            self.reverse_open = False
            return AttachResult("key-rename", key)
        elif kind == "key-remove":
            self.reverse_stage = "key-delete"
        elif kind in ("key-unregister", "key-delete-files") \
                and "delete" in self.key_actions:
            self._flash(self.key_actions["delete"](
                key, kind == "key-delete-files"))
            self._load_keys()
            self._pending_key = None
            self.reverse_stage = "keys"
        elif kind == "key-back":
            self.reverse_stage = "key"
        else:
            return None
        self.reverse_sel = 0
        self.reverse_scroll = 0
        return None

    def _key_back(self):
        if self.reverse_stage in ("key-host", "key-delete"):
            self.reverse_stage = "key"
        elif self.reverse_stage == "key":
            self.reverse_stage = "keys"
            self._pending_key = None
        else:
            self.reverse_open = False
        self.reverse_sel = 0
        self.reverse_scroll = 0

    def _reverse_rows(self):
        if self.reverse_stage in self.KEY_STAGES:
            return self._key_rows()
        if self.reverse_stage == "menu" and self._pending_host:
            host = self._pending_host
            state = self._host_status.get(
                host.get("address") or host.get("name") or "", {})                 .get("state", "")
            suffix = " · " + state if state else ""
            return [
                {"kind": "ssh", "label": "SSH 접속 (터미널 열기)"},
                {"kind": "reverse-open",
                 "label": "리버스 에이전트 실행 →"},
                {"kind": "check", "label": "키·연결 확인" + suffix},
                {"kind": "install", "label": "공개키 서버에 설치"},
            ]
        if not self._pending_host and not self._pending_local:
            return [{"kind": "none",
                     "label": "저장된 호스트가 없습니다"}]
        if self._pending_local:
            target = "대상: 로컬 샌드박스"
        else:
            host = self._pending_host or {}
            target = "대상: %s — %s" % (host.get("name", "?"),
                                        host.get("address", ""))
            cached = self._host_status.get(
                host.get("address") or host.get("name") or "", {})
            if str(cached.get("state", "")).startswith("연결 실패"):
                target += " · ✗ " + cached["state"]
        rows = [{"kind": "target", "label": target + "  (Tab 변경)"}]
        providers = ["fake"] if self._pending_local else \
            ["claude", "codex", "grok"]
        for provider in providers:
            rows.append({"kind": "provider",
                         "label": provider + " 실행",
                         "provider": provider})
        return rows

    def _reverse_activate(self):
        rows = self._reverse_rows()
        if not rows or self.reverse_sel >= len(rows):
            return None
        row = rows[self.reverse_sel]
        if self.reverse_stage in self.KEY_STAGES:
            result = self._key_activate(row)
            self._focus_dirty = True
            return result
        if row["kind"] == "none":
            pass
        elif row["kind"] == "ssh":
            self.reverse_open = False
            return AttachResult("ssh-connect", self._pending_host)
        elif row["kind"] == "reverse-open":
            self.reverse_stage = "provider"
            self.reverse_sel = 0
        elif row["kind"] == "check":
            self.reverse_open = False
            return AttachResult("host-check", self._pending_host)
        elif row["kind"] == "install":
            self.reverse_open = False
            return AttachResult("host-install-key", self._pending_host)
        elif row["kind"] == "target":
            self._cycle_target()
        elif row["kind"] == "provider":
            root = None
            if self.explorer_root_provider:
                try:
                    root = self.explorer_root_provider(self.active_id)
                except Exception:
                    root = None
            if not root:
                active = self._active()
                root = active.pane.get("cwd") if active else os.getcwd()
            self.reverse_open = False
            return AttachResult("reverse-start", {
                "local": self._pending_local, "host": self._pending_host,
                "provider": row["provider"], "client_root": root})
        self._focus_dirty = True
        return None

    def _reverse_key(self, unit):
        if unit.startswith(b"\x1b"):
            final = unit[-1:]
            rows = self._reverse_rows()
            if final == b"A":
                self.reverse_sel = max(0, self.reverse_sel - 1)
            elif final == b"B":
                self.reverse_sel = min(max(0, len(rows) - 1),
                                       self.reverse_sel + 1)
            self._focus_dirty = True
            return None
        key = latin_key(unit.decode(errors="ignore"))
        if key in ("q", "\x1b"):
            if self.reverse_stage in self.KEY_STAGES:
                self._key_back()
            elif self.reverse_stage == "provider" and self._pending_host:
                self.reverse_stage = "menu"  # back to the host menu
                self.reverse_sel = 0
            else:
                self.reverse_open = False
        elif key == "\t":
            if self.reverse_stage not in self.KEY_STAGES:
                self._cycle_target()
        elif key in ("\r", "\n"):
            return self._reverse_activate()
        elif key == "j":
            self.reverse_sel = min(
                max(0, len(self._reverse_rows()) - 1),
                self.reverse_sel + 1)
        elif key == "k":
            self.reverse_sel = max(0, self.reverse_sel - 1)
        self._focus_dirty = True
        return None

    def _reverse_click(self, button, gx, gy, final):
        _x, _y, area_w, area_h = self._area()
        width = min(40, area_w)
        if button in (64, 65):
            self.reverse_sel = max(0, min(
                max(0, len(self._reverse_rows()) - 1),
                self.reverse_sel + (3 if button == 65 else -3)))
            self._focus_dirty = True
            return
        if final != b"M":
            return
        if gx - self._area()[0] > width or gy > area_h:
            self.reverse_open = False
            self._focus_dirty = True
            return
        row = gy - 3  # title + subtitle rows
        if row < 0:
            return
        index = self.reverse_scroll + row
        if index >= len(self._reverse_rows()):
            return
        now = time.time()
        last_time, last_index = self._last_click
        self.reverse_sel = index
        self._focus_dirty = True
        if last_index == ("reverse", index) and now - last_time < 0.4:
            result = self._reverse_activate()
            if result is not None:
                self._explorer_result = result
        self._last_click = (now, ("reverse", index))

    def _reverse_chunk(self):
        _x, _y, area_w, area_h = self._area()
        width = min(40, area_w)
        inner = width - 2
        out = []
        col = self._area()[0] + 1
        bg = b"\x1b[0;48;5;231m"
        edge = bg + b"\x1b[38;5;250m"
        keys = self.reverse_stage in self.KEY_STAGES
        title = " SSH 키 " if keys else " 호스트 " \
            if self.reverse_stage == "menu" else " 리버스 에이전트 "
        out.append(("\x1b[1;%dH" % col).encode() + edge + "┌".encode()
                   + bg + b"\x1b[1;38;5;31m" + title.encode() + edge
                   + ("─" * max(0, inner - self._width(title))
                      + "┐").encode() + b"\x1b[0m")
        key_name = (self._pending_key or {}).get("name", "")
        if self.reverse_stage == "keys":
            subtitle = " 관리 키 + ~/.ssh 자동 발견"
        elif self.reverse_stage == "key":
            subtitle = " " + key_name
        elif self.reverse_stage == "key-host":
            subtitle = " %s 공개키를 설치할 호스트" % key_name
        elif self.reverse_stage == "key-delete":
            subtitle = " %s 삭제" % key_name
        elif self.reverse_stage == "menu" and self._pending_host:
            subtitle = " %s — %s" % (self._pending_host.get("name", "?"),
                                      self._pending_host.get("address", ""))
        elif not self._pending_host and not self._pending_local:
            subtitle = " 왼쪽 탭에서 '+ 호스트 추가…'를 먼저 눌러주세요"
        else:
            target = "로컬 샌드박스" if self._pending_local else \
                (self._pending_host or {}).get("name", "호스트")
            subtitle = " %s에서 실행할 에이전트" % target
        out.append(("\x1b[2;%dH" % col).encode() + edge + "│".encode()
                   + bg + b"\x1b[38;5;242m"
                   + self._pad(subtitle, inner).encode()
                   + edge + "│".encode() + b"\x1b[0m")
        rows = self._reverse_rows()
        visible = max(1, area_h - 3)
        if self.reverse_sel < self.reverse_scroll:
            self.reverse_scroll = self.reverse_sel
        if self.reverse_sel >= self.reverse_scroll + visible:
            self.reverse_scroll = self.reverse_sel - visible + 1
        for row in range(visible):
            index = self.reverse_scroll + row
            if index < len(rows):
                label = rows[index]["label"]
                selected = index == self.reverse_sel
            else:
                label, selected = "", False
            info = index < len(rows) and rows[index]["kind"] == "info"
            style = b"\x1b[0;1;48;5;195;38;5;16m" if selected else \
                bg + (b"\x1b[38;5;245m" if info else b"\x1b[38;5;238m")
            prefix = "▸" if selected else " "
            out.append(("\x1b[%d;%dH" % (row + 3, col)).encode() + edge
                       + "│".encode() + style
                       + self._pad(prefix + label, inner).encode()
                       + edge + "│".encode() + b"\x1b[0m")
        hint = " Enter 열기 · q 닫기 " if self.reverse_stage == "keys" \
            else " Enter 선택 · q 뒤로 " if keys \
            else " Enter 시작 · Tab 대상 변경 · q 닫기 "
        out.append(("\x1b[%d;%dH" % (area_h, col)).encode() + edge
                   + "└".encode() + bg + b"\x1b[38;5;242m" + hint.encode()
                   + edge + ("─" * max(0, inner - self._width(hint))
                             + "┘").encode() + b"\x1b[0m")
        return b"".join(out)

    # -- file explorer ------------------------------------------------------
    def _toggle_explorer(self):
        self.reverse_open = False
        if self.explorer_open:
            self.explorer_open = False
        else:
            root = None
            if self.explorer_root_provider:
                try:
                    root = self.explorer_root_provider(self.active_id)
                except Exception:
                    root = None
            if not root:
                active = self._active()
                root = active.pane.get("cwd") if active else os.getcwd()
            self.explorer_path = os.path.realpath(
                os.path.expanduser(root))
            if not os.path.isdir(self.explorer_path):
                self.explorer_path = os.path.expanduser("~")
            self._explorer_load()
            self.explorer_open = True
            self.explorer_focus = True
        self._focus_dirty = True

    def _explorer_load(self):
        entries = []
        try:
            names = os.listdir(self.explorer_path)
        except OSError:
            names = []
        for name in sorted(names, key=lambda n: n.lower()):
            path = os.path.join(self.explorer_path, name)
            entries.append((name, os.path.isdir(path), path))
        entries.sort(key=lambda item: (not item[1], item[0].lower()))
        parent = os.path.dirname(self.explorer_path)
        if parent != self.explorer_path:
            entries.insert(0, ("..", True, parent))
        self.explorer_entries = entries
        self.explorer_sel = min(self.explorer_sel,
                                max(0, len(entries) - 1))
        self.explorer_scroll = 0

    def _explorer_open_entry(self):
        if not self.explorer_entries:
            return None
        _name, is_dir, path = self.explorer_entries[self.explorer_sel]
        if is_dir:
            self.explorer_path = path
            self.explorer_sel = 0
            self._explorer_load()
            self._focus_dirty = True
            return None
        self.explorer_open = False
        return AttachResult("open-file", path)

    def _explorer_key(self, unit):
        if unit.startswith(b"\x1b"):
            final = unit[-1:]
            if final == b"A":
                self.explorer_sel = max(0, self.explorer_sel - 1)
            elif final == b"B":
                self.explorer_sel = min(len(self.explorer_entries) - 1,
                                        self.explorer_sel + 1)
            elif final == b"C":
                return self._explorer_open_entry()
            elif final == b"D":
                parent = os.path.dirname(self.explorer_path)
                if parent != self.explorer_path:
                    self.explorer_path = parent
                    self.explorer_sel = 0
                    self._explorer_load()
            self._focus_dirty = True
            return None
        key = latin_key(unit.decode(errors="ignore"))
        if key in ("q", "\x1b"):
            self.explorer_open = False
        elif key in ("\r", "\n"):
            return self._explorer_open_entry()
        elif key == "j":
            self.explorer_sel = min(len(self.explorer_entries) - 1,
                                    self.explorer_sel + 1)
        elif key == "k":
            self.explorer_sel = max(0, self.explorer_sel - 1)
        elif key in ("\x7f", "h"):
            parent = os.path.dirname(self.explorer_path)
            if parent != self.explorer_path:
                self.explorer_path = parent
                self.explorer_sel = 0
                self._explorer_load()
        elif key == "r":
            self._explorer_load()
        self._focus_dirty = True
        return None

    def _explorer_click(self, button, gx, gy, final):
        area_h = self._area()[3]
        if button in (64, 65):
            self.explorer_sel = max(0, min(
                len(self.explorer_entries) - 1,
                self.explorer_sel + (3 if button == 65 else -3)))
            self._focus_dirty = True
            return
        if final != b"M" or gy > area_h:
            return
        row = gy - 2  # row 1 is the panel title
        if row < 0:
            return
        index = self.explorer_scroll + row
        if index >= len(self.explorer_entries):
            return
        now = time.time()
        last_time, last_index = self._last_click
        self.explorer_sel = index
        self._focus_dirty = True
        if last_index == index and now - last_time < 0.4:
            result = self._explorer_open_entry()
            if result is not None:
                self._explorer_result = result
        self._last_click = (now, index)

    _explorer_result = None

    HELP_ANIM = 0.14  # seconds for the pop-in

    def _help_animating(self):
        return self.help_open and not self._help_full

    def _help_lines(self, inner):
        """[(text, style)] rows inside the popup box."""
        key_w = max(self._width(k) for k, _ in HELP_KEYS) + 4
        col_w = key_w + max(self._width(d) for _, d in HELP_KEYS) + 2
        columns = 2 if inner >= col_w * 2 + 2 else 1
        per_col = -(-len(HELP_KEYS) // columns)
        rows = [("", None),
                ("  Ctrl-B 를 누른 다음:", "38;5;250")]
        rows.append(("", None))
        for i in range(per_col):
            parts = []
            for c in range(columns):
                index = c * per_col + i
                if index < len(HELP_KEYS):
                    key, desc = HELP_KEYS[index]
                    parts.append((self._pad("  " + key, key_w),
                                  self._pad(desc, col_w - key_w)))
            rows.append((parts, "keys"))
        rows.append(("", None))
        rows.append(("  마우스: pane 클릭 = 포커스 · 하단 탭 클릭 = 윈도우 전환",
                     "38;5;245"))
        rows.append(("  왼쪽 탭에서 세션 · 호스트 클릭으로 바로 이동",
                     "38;5;245"))
        rows.append(("  Ctrl-V: 클립보드 이미지를 원격 pane에 경로로 첨부",
                     "38;5;245"))
        rows.append(("  사이드바 SSH 키: 생성·공개키 복사·서버 설치·이름 변경·삭제",
                     "38;5;245"))
        return rows

    def _help_chunk(self):
        cols, rows = self._winsize()
        full_w = min(68, cols - 2)
        inner = full_w - 2
        lines = self._help_lines(inner)
        full_h = min(len(lines) + 3, rows - 1)
        elapsed = time.time() - self._help_opened_at
        t = min(1.0, max(0.0, elapsed / self.HELP_ANIM))
        grow = 1 - (1 - t) ** 3  # ease-out
        width = max(10, int(round(full_w * (0.6 + 0.4 * grow))))
        height = max(3, int(round(full_h * (0.6 + 0.4 * grow))))
        width, height = min(width, full_w), min(height, full_h)
        inner = width - 2
        left = max(0, (cols - width) // 2)
        top = max(0, (rows - 1 - height) // 2)
        box = "\x1b[0;48;5;235;38;5;51m"
        body = "\x1b[0;48;5;235;38;5;252m"
        out = []

        def put(row, text):
            out.append("\x1b[%d;%dH" % (top + row + 1, left + 1) + text)

        title = " crow-cli 단축키 "
        put(0, box + "╭" + self._pad("─" + title, inner, "─") + "╮")
        if t >= 1.0:
            visible = lines[:height - 3]
            self._help_full = True
        else:
            visible = [("", None)] * (height - 3)
        for i, (content, style) in enumerate(visible):
            if style == "keys":
                text = ""
                used = 0
                for key, desc in content:
                    text += ("\x1b[1;48;5;235;38;5;51m" + key
                             + body + desc)
                    used += self._width(key) + self._width(desc)
                text += " " * max(0, inner - used)
            else:
                text = ("\x1b[0;48;5;235;%sm" % style if style else body) \
                    + self._pad(content, inner)
            put(i + 1, box + "│" + text + box + "│")
        hint = "아무 키나 누르면 닫힘 "
        put(height - 2, box + "│" + body + " " * inner + box + "│")
        if self._width(hint) + 2 > inner:
            hint = ""
        dashes = "─" * (inner - self._width(hint) - (1 if hint else 0))
        put(height - 1, box + "╰" + dashes + (" " if hint else "")
            + "\x1b[38;5;245m" + hint + box + "╯")
        shadow = "\x1b[0;48;5;238m"
        for row in range(1, height):
            if left + width < cols:
                out.append("\x1b[%d;%dH" % (top + row + 1, left + width + 1)
                           + shadow + " ")
        if top + height < rows - 1:
            out.append("\x1b[%d;%dH" % (top + height + 1, left + 2) + shadow
                       + " " * max(0, min(width, cols - left - 1)))
        return ("".join(out) + "\x1b[0m").encode(errors="replace")

    # -- host add form ----------------------------------------------------------
    FORM_FIELDS = ("address", "port", "key", "save", "cancel")

    def _open_host_form(self):
        keys = [("기본 키 자동 탐색", None)]
        if self.host_form:
            keys += list(self.host_form["keys"]())
        self.form = {"address": "", "port": "", "keys": keys, "key": 0,
                     "focus": 0, "error": None, "opened": time.time(),
                     "cursor": None, "hits": []}
        self.explorer_open = False
        self.reverse_open = False
        self.help_open = False
        if self._ime_saved is None:  # addresses are typed in Latin
            self._ime_saved = imswitch.to_latin()
        self._focus_dirty = True

    def _close_host_form(self):
        self.form = None
        self._restore_ime()
        self._focus_dirty = True

    def _form_animating(self):
        return self.form is not None and not self.form.get("full")

    def _form_submit(self):
        form = self.form
        address = form["address"].strip()
        if not address:
            form["error"] = "SSH 주소를 입력하세요 (user@host 또는 host)"
            form["focus"] = 0
            return None
        identity = form["keys"][form["key"]][1]
        try:
            record = self.host_form["save"](address, form["port"].strip(),
                                            identity)
        except ValueError as exc:
            form["error"] = str(exc)
            return None
        self._close_host_form()
        self._sidebar_at = 0.0
        self._open_reverse_for_host(record)
        return None

    def _form_key(self, unit):
        form = self.form
        mouse = MOUSE_RE.match(unit)
        if mouse:
            if mouse.group(4) == b"M" and not int(mouse.group(1)) & 96:
                gx, gy = int(mouse.group(2)), int(mouse.group(3))
                for row, x0, x1, field in form["hits"]:
                    if gy == row and x0 <= gx < x1:
                        form["focus"] = self.FORM_FIELDS.index(field)
                        if field == "save":
                            return self._form_submit()
                        if field == "cancel":
                            self._close_host_form()
                            return None
                        if field == "key":
                            form["key"] = (form["key"] + 1) % len(form["keys"])
                        return None
                y0, x0, y1, x1 = form.get("box") or (0, 0, 10 ** 6, 10 ** 6)
                if not (y0 <= gy <= y1 and x0 <= gx <= x1):
                    self._close_host_form()  # click outside = cancel
            return None
        field = self.FORM_FIELDS[form["focus"]]
        if unit == b"\x1b":
            self._close_host_form()
            return None
        if unit in (b"\r", b"\n"):
            if field == "cancel":
                self._close_host_form()
                return None
            return self._form_submit()
        if unit in (b"\t", b"\x1b[B"):
            form["focus"] = (form["focus"] + 1) % len(self.FORM_FIELDS)
            return None
        if unit in (b"\x1b[Z", b"\x1b[A"):
            form["focus"] = (form["focus"] - 1) % len(self.FORM_FIELDS)
            return None
        if unit in (b"\x1b[C", b"\x1b[D"):
            step = 1 if unit == b"\x1b[C" else -1
            if field == "key":
                form["key"] = (form["key"] + step) % len(form["keys"])
            elif field in ("save", "cancel"):
                form["focus"] = 3 if field == "cancel" else 4
            return None
        if field not in ("address", "port"):
            return None
        if unit in (b"\x7f", b"\x08"):
            form[field] = form[field][:-1]
            form["error"] = None
            return None
        if unit.startswith(b"\x1b"):
            return None  # bracketed-paste markers and other sequences
        text = unit.decode(errors="ignore")
        if field == "port":
            text = "".join(c for c in text if c.isdigit())
        elif text.isspace():
            text = ""
        if text and text.isprintable():
            form[field] += text
            form["error"] = None
        return None

    def _form_lines(self, form, top, left, inner, body, dim):
        """Rows inside the box, each styled and exactly `inner` wide.
        Records click targets and the text cursor (1-based screen cells)."""
        label_w, field_w = 11, inner - 15
        focus = self.FORM_FIELDS[form["focus"]]
        lines = []

        def plain(text, style=body):
            lines.append(style + self._pad(text, inner))

        def field(label, name, shown, empty):
            on = focus == name
            row = top + len(lines) + 2
            col = left + 4 + label_w  # 0-based column of the input box
            form["hits"].append((row, col + 1, col + 1 + field_w, name))
            base = "\x1b[0;48;5;%d;38;5;%dm" % ((238, 255) if on
                                               else (237, 250))
            text_style = "\x1b[0;48;5;%d;38;5;242m" % (238 if on else 237) \
                if empty else base
            while self._width(shown) > field_w - 2 and len(shown) > 1:
                shown = shown[1:]  # keep the tail visible while typing
            if on and name != "key":
                form["cursor"] = (row, col + 1
                                  + (0 if empty else self._width(shown)))
            lines.append(body + "  "
                         + ("\x1b[1;48;5;235;38;5;51m" if on else body)
                         + self._pad(label, label_w) + text_style + " "
                         + self._pad(shown, field_w - 1) + body
                         + " " * (inner - 2 - label_w - field_w))

        address = form["address"].strip()
        derived = address and self.host_form and \
            self.host_form["derive"](address)
        key_label, key_path = form["keys"][form["key"]]
        plain("")
        plain("  저장하면 왼쪽 탭에서 바로 접속 · 리버스 할 수 있어요", dim)
        plain("")
        field("SSH 주소", "address", form["address"] or "user@host 또는 host",
              not form["address"])
        field("포트", "port", form["port"] or "22", not form["port"])
        field("키", "key", "‹ %s ›" % self._pad(key_label, field_w - 6).rstrip(),
              False)
        plain("")
        plain("  이름: %s" % (derived or "주소에서 자동으로 정해져요")
              + ("   키 파일: %s" % key_path if key_path else ""), dim)
        if form["error"]:
            plain("  " + form["error"], "\x1b[0;48;5;235;38;5;203m")
        else:
            plain("")
        plain("")
        row = top + len(lines) + 2
        col = left + 1 + max(1, (inner - 24) // 2)  # 0-based
        text = body + " " * (col - left - 1)
        for name, label in (("save", "  저장  "), ("cancel", "  취소  ")):
            style = "\x1b[0;1;48;5;45;38;5;16m" if focus == name else \
                "\x1b[0;48;5;244;38;5;231m"
            form["hits"].append((row, col, col + 2 + self._width(label),
                                 name))
            text += style + label + body + "    "
            col += self._width(label) + 4
        lines.append(text + " " * max(0, inner - (col - left - 1)))
        return lines

    def _form_chunk(self):
        form = self.form
        cols, rows = self._winsize()
        full_w = min(60, cols - 2)
        full_h = 13
        elapsed = time.time() - form["opened"]
        t = min(1.0, max(0.0, elapsed / self.HELP_ANIM))
        grow = 1 - (1 - t) ** 3
        width = min(full_w, max(10, int(round(full_w * (0.6 + 0.4 * grow)))))
        height = min(full_h, max(3, int(round(full_h * (0.6 + 0.4 * grow)))),
                     rows - 1)
        inner = width - 2
        left = max(0, (cols - width) // 2)
        top = max(0, (rows - 1 - height) // 2)
        box = "\x1b[0;48;5;235;38;5;51m"
        body = "\x1b[0;48;5;235;38;5;252m"
        dim = "\x1b[0;48;5;235;38;5;245m"
        out = []
        form["hits"] = []
        form["cursor"] = None

        def put(row, text):
            out.append("\x1b[%d;%dH" % (top + row + 1, left + 1) + text)

        put(0, box + "╭" + self._pad("─ 호스트 추가 ", inner, "─") + "╮")
        blank = body + " " * inner
        lines = [blank] * (height - 2)
        if t >= 1.0:
            lines = self._form_lines(form, top, left, inner, body, dim)
            form["full"] = True
        for i, text in enumerate(lines[:height - 2]):
            put(i + 1, box + "│" + text + box + "│")
        hint = "Tab 이동 · ←→ 키 선택 · Enter 저장 · Esc 취소 "
        if self._width(hint) + 2 > inner:
            hint = ""
        dashes = "─" * (inner - self._width(hint) - (1 if hint else 0))
        put(height - 1, box + "╰" + dashes + (" " if hint else "")
            + "\x1b[38;5;245m" + hint + box + "╯")
        form["box"] = (top + 1, left + 1, top + height, left + width)
        if hint and t >= 1.0:
            start = left + 3 + len(dashes)  # 1-based column of the hint
            for word, name in (("Enter 저장", "save"), ("Esc 취소", "cancel")):
                x0 = start + self._width(hint[:hint.index(word)])
                form["hits"].append((top + height, x0,
                                     x0 + self._width(word), name))
        shadow = "\x1b[0;48;5;238m"
        for row in range(1, height):
            if left + width < cols:
                out.append("\x1b[%d;%dH" % (top + row + 1, left + width + 1)
                           + shadow + " ")
        if top + height < rows - 1:
            out.append("\x1b[%d;%dH" % (top + height + 1, left + 2) + shadow
                       + " " * max(0, min(width, cols - left - 1)))
        return ("".join(out) + "\x1b[0m").encode(errors="replace")

    def _explorer_chunk(self):
        _x, _y, _w, area_h = self._area()
        width = self._sidebar_span()[0]
        height = area_h
        inner = width - 2
        out = []
        col = 1
        title = " Files: " + (self.explorer_path or "")
        if self._width(title) > inner:  # keep the path's tail, by width
            kept, used = "", 0
            for char in reversed(self.explorer_path or ""):
                w = char_width(char)
                if used + w > inner - 9:
                    break
                kept = char + kept
                used += w
            title = " Files: …" + kept
        bg = b"\x1b[0;48;5;231m"
        edge = bg + b"\x1b[38;5;250m"
        title_style = b"\x1b[1;38;5;31m" if self.explorer_focus \
            else b"\x1b[38;5;245m"
        out.append(("\x1b[1;%dH" % col).encode() + edge + "┌".encode()
                   + bg + title_style + title.encode() + edge
                   + ("─" * max(0, inner - self._width(title))
                      + "┐").encode() + b"\x1b[0m")
        visible = max(1, height - 2)
        if self.explorer_sel < self.explorer_scroll:
            self.explorer_scroll = self.explorer_sel
        if self.explorer_sel >= self.explorer_scroll + visible:
            self.explorer_scroll = self.explorer_sel - visible + 1
        for row in range(visible):
            index = self.explorer_scroll + row
            if index < len(self.explorer_entries):
                name, is_dir, _path = self.explorer_entries[index]
                label = name + ("/" if is_dir else "")
                selected = index == self.explorer_sel
            else:
                label, selected = "", False
            selected_style = b"\x1b[0;1;48;5;195;38;5;16m" \
                if self.explorer_focus else b"\x1b[0;48;5;254;38;5;238m"
            style = selected_style if selected else bg + b"\x1b[38;5;238m"
            out.append(("\x1b[%d;%dH" % (row + 2, col)).encode() + edge
                       + "│".encode() + style
                       + self._pad(label, inner).encode()
                       + edge + "│".encode() + b"\x1b[0m")
        out.append(("\x1b[%d;%dH" % (height, col)).encode() + edge
                   + ("└" + ("─" * inner) + "┘").encode() + b"\x1b[0m")
        gap = ("\x1b[%%d;%dH " % (width + 1)).encode()  # pane-side margin
        out += [gap % (row + 1) for row in range(height)]
        return b"".join(out)

    REPEAT_TIME = 0.5

    def _prefix_unit(self, unit, rects):
        if unit == PREFIX:
            active = self._active()
            if active is not None:
                active.conn.send("D", PREFIX)
            return None
        if unit.startswith(b"\x1b"):
            # Arrow keys arrive as CSI/SS3 sequences.
            final = unit[-1:]
            direction = {b"A": "up", b"B": "down", b"C": "right",
                         b"D": "left"}.get(final)
            if direction and self._focus_direction(direction, rects):
                self._focus_dirty = True
            if direction:
                self._repeat_until = time.time() + self.REPEAT_TIME
            return None
        key = latin_key(unit.decode(errors="ignore"))
        if key == "d":
            return AttachResult("detach")
        if key == "%":
            return AttachResult("split-v", self.active_id)
        if key == '"':
            return AttachResult("split-h", self.active_id)
        if key in ("o", "\t"):
            if self._focus_cycle(rects):
                self._focus_dirty = True
            return None
        if key == "z":
            return AttachResult("zoom", self.active_id)
        if key == "e":
            self._toggle_explorer()
            return None
        if key == "a":
            self._toggle_reverse()
            return None
        if key == "c":
            return AttachResult("new")
        if key == "n":
            return AttachResult("switch", 1)
        if key == "p":
            return AttachResult("switch", -1)
        if key.isdigit():
            return AttachResult("switch", ("index", int(key)))
        if key == "s":
            return AttachResult("choose")
        if key == ",":
            return AttachResult("rename-window")
        if key == "$":
            return AttachResult("rename-session")
        if key == "x":
            return AttachResult("kill-pane", self.active_id)
        if key == "?":
            self.help_open = True
            self._help_opened_at = time.time()
            self._help_full = False
            self._focus_dirty = True
            return None
        return None  # unknown prefix keys are swallowed, like tmux
