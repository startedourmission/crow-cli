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

from . import clipimage, daemon as daemon_mod, imswitch, outtrace, \
    registry as registry_mod
from .vt import DEFAULT_ATTR, Screen, char_width, sgr_for

PREFIX = b"\x02"  # Ctrl-B, like tmux

HELP_KEYS = [
    ("d", "detach (세션 유지)"),
    ("%", "좌우로 분할"),
    ('"', "위아래로 분할"),
    ("←↑↓→ o", "pane 이동 (방향키는 0.5초 안에 연타 가능)"),
    ("z", "pane 확대 / 복원"),
    ("{  }", "pane 자리 바꾸기 (앞 / 뒤 pane과)"),
    ("!", "pane을 새 윈도우로 떼어내기"),
    ("x", "pane 닫기"),
    ("c", "새 윈도우"),
    ("n  p", "다음 / 이전 윈도우"),
    ("0-9", "윈도우 선택"),
    ("s", "세션 선택 (목록 팝업)"),
    (",  $", "윈도우 / 세션 이름"),
    ("e", "파일 탐색기"),
    ("a", "호스트 · 리버스"),
    ("f", "리버스 pane 작업 폴더 바꾸기 (새 대화)"),
    ("h", "사이드바 키보드 조작 (호스트·세션·키)"),
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


def split_dividers(tree, x, y, w, h):
    """The divider strip of every split, mirroring compute_rects:
    [(node, split, (x, y, w, h) of the strip, origin, span)] (0-based).
    Dragging a strip sets that node's ratio."""
    if not tree or "leaf" in tree or w <= 0 or h <= 0:
        return []
    gap = DIVIDER
    ratio = tree.get("ratio", 0.5)
    if tree["split"] == "v":
        if w <= gap + 1:
            return []
        first = max(1, int((w - gap) * ratio))
        return ([(tree, "v", (x + first, y, gap, h), x, w)]
                + split_dividers(tree["a"], x, y, first, h)
                + split_dividers(tree["b"], x + first + gap, y,
                                 max(1, w - first - gap), h))
    if h <= gap + 1:
        return []
    first = max(1, int((h - gap) * ratio))
    return ([(tree, "h", (x, y + first, w, gap), y, h)]
            + split_dividers(tree["a"], x, y, w, first)
            + split_dividers(tree["b"], x, y + first + gap, w,
                             max(1, h - first - gap)))


def drag_ratio(position, origin, span, minimum=2):
    """Ratio that puts a split's divider at `position` (0-based cell),
    keeping at least `minimum` cells on either side."""
    room = span - DIVIDER
    if room <= 2 * minimum:
        return 0.5
    first = max(minimum, min(room - minimum, position - origin))
    return round(float(first) / room, 4)


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

    def consume(self):
        """Apply buffered frames: (screen changed, exit code or None)."""
        changed = False
        for kind, payload in self.conn.frames():
            if kind == "O":
                if self.awaiting_replay and payload != b"\x1bc":
                    self.awaiting_replay = False
                    self.screen.feed_replay(payload)
                    # queries in old output were answered back then
                    self.screen.responses = []
                else:
                    self.screen.feed(payload)
                changed = True
                for response in self.screen.responses:
                    try:
                        self.conn.send("D", response)
                    except socket.error:
                        pass
                self.screen.responses = []
            elif kind == "X":
                code = struct.unpack(">I", payload[:4])[0] \
                    if len(payload) >= 4 else 0
                return changed, code
        return changed, None


class PaneCache(object):
    """Pane connections and screens kept alive between the Client runs
    of one crow process (window switch, split, zoom), so coming back
    needs no replay. A background thread keeps parked screens current."""

    def __init__(self):
        self.lock = threading.Lock()
        self.parked = {}
        self.live = set()  # pane ids the attached Client owns right now
        self.thread = None

    def park(self, runtime):
        if runtime.conn is None:
            return
        with self.lock:
            old = self.parked.pop(runtime.id, None)
            self.parked[runtime.id] = runtime
        if old is not None and old.conn is not runtime.conn:
            old.conn.close()
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def take(self, pane_id):
        with self.lock:
            return self.parked.pop(pane_id, None)

    def prefetch(self, entries):
        """Connect and replay panes of windows not on screen yet.
        entries: (pane, window, (cols, rows)) with the size the pane's
        pty already has, so the replayed screen lines up."""
        for pane, window, size in entries:
            with self.lock:
                if pane["id"] in self.parked or pane["id"] in self.live:
                    continue
            runtime = PaneRuntime(pane, window)
            if size:
                runtime.screen.resize(*size)
            if not runtime.connect():
                continue
            try:
                runtime.conn.send("A")
                runtime.conn.sock.setblocking(False)
            except socket.error:
                runtime.conn.close()
                continue
            runtime.awaiting_replay = True
            self.park(runtime)

    def _drop(self, pane_id):
        runtime = self.parked.pop(pane_id, None)
        if runtime is not None:
            runtime.conn.close()

    def _run(self):
        while True:
            with self.lock:
                socks = dict((r.conn.sock, r.id)
                             for r in self.parked.values())
            if not socks:
                time.sleep(0.1)
                continue
            try:
                ready = select.select(list(socks), [], [], 0.1)[0]
            except (OSError, ValueError):  # a socket closed under us
                time.sleep(0.05)
                continue
            with self.lock:
                for sock in ready:
                    runtime = self.parked.get(socks[sock])
                    if runtime is None or runtime.conn.sock is not sock:
                        continue  # taken back by the attached client
                    try:
                        data = sock.recv(65536)
                    except BlockingIOError:
                        continue
                    except socket.error:
                        data = b""
                    if not data:
                        self._drop(runtime.id)
                        continue
                    runtime.conn.feed(data)
                    if runtime.consume()[1] is not None:
                        self._drop(runtime.id)


PANE_CACHE = PaneCache()


class Client(object):
    def __init__(self, hooks, status_provider=None,
                 explorer_root_provider=None, reverse_data_provider=None,
                 sidebar_data_provider=None, notice_provider=None,
                 windows_provider=None, host_form=None, image_target=None,
                 key_actions=None, layout_saver=None):
        self.hooks = hooks
        # (window_id, layout tree) -> None: persists a dragged divider
        self.layout_saver = layout_saver
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
    PREFETCH_DELAY = 1.0

    def _prefetch(self, windows):
        """Warm PANE_CACHE with the session's other windows once this
        one is on screen, so the first switch to them is instant too."""
        entries = []
        for win in windows:
            panes = registry_mod.window_panes(win)
            rects = compute_rects(win.get("layout"),
                                  dict((p["id"], p) for p in panes),
                                  *self._area(), zoomed=win.get("zoomed"))
            for pane in panes:
                rect = rects.get(pane["id"])
                entries.append((pane, win, (rect[2], rect[3]) if rect
                                else None))
        PANE_CACHE.prefetch(entries)

    def attach(self, session_name, window, others=()):
        """Block until detach / window exit / switch / split request.
        `others`: the session's other windows, preloaded in the
        background."""
        if others:
            timer = threading.Timer(self.PREFETCH_DELAY, self._prefetch,
                                    args=(list(others),))
            timer.daemon = True
            timer.start()
        self._session_name = session_name
        self._window = window
        panes = registry_mod.window_panes(window)
        if not panes:
            raise RuntimeError("window has no panes")
        active = registry_mod.active_pane(window)
        self.active_id = active["id"] if active else panes[0]["id"]
        self.runtimes = []
        with PANE_CACHE.lock:
            PANE_CACHE.live = set(p["id"] for p in panes)
        for pane in panes:
            runtime = PaneRuntime(pane, window)
            parked = PANE_CACHE.take(pane["id"])
            if parked is not None:
                runtime.conn, runtime.screen = parked.conn, parked.screen
                runtime.awaiting_replay = parked.awaiting_replay
                runtime.cached = True
            self.runtimes.append(runtime)
        missing = []
        for runtime in self.runtimes:
            if getattr(runtime, "cached", False):
                continue
            if not runtime.connect():
                code = daemon_mod.has_exited(runtime.id,
                                             root=runtime.root())
                if code is not None:
                    for other in self.runtimes:
                        if other.conn is not None:
                            PANE_CACHE.park(other)
                    return AttachResult("dead", (runtime.id, code))
                missing.append(runtime.pane["name"])
        if missing:
            raise RuntimeError("pane %s is not running" % missing[0])
        rects = self._rects()
        stdin_fd = sys.stdin.fileno()
        old = termios.tcgetattr(stdin_fd)
        self._resize = False
        old_handler = signal.signal(signal.SIGWINCH, self._on_resize)
        out = outtrace.Writer(sys.stdout.buffer)
        outtrace.install()
        outtrace.note("attach", "%s:%s" % (session_name, window.get("name")))
        result = None
        try:
            tty.setraw(stdin_fd)
            _, rows = self._winsize()
            # Reserve the last row: the outer terminal's scroll region is
            # everything above it, so nothing (scrolling, full-width
            # writes, app redraws) can ever roll over the status bar.
            region = ("\x1b[1;%dr" % max(1, rows - 1)).encode()
            if Client._term_active:
                # Straight back from a window switch: same screen, modes
                # still on; just wipe the old window's cells.
                out.write(b"\x1b[?25l" + region + b"\x1b[2J")
            else:
                out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1002h\x1b[?1006h"
                          + region)
            Client._term_active = True
            out.flush()
            self._probe_outer_colors(out, stdin_fd)
            for runtime in self.runtimes:
                rect = rects.get(runtime.id)
                if rect and (runtime.screen.cols, runtime.screen.rows) \
                        != (rect[2], rect[3]):
                    runtime.screen.resize(rect[2], rect[3])
                if not getattr(runtime, "cached", False):
                    runtime.awaiting_replay = True
                    runtime.conn.send("A")
                if rect:
                    runtime.conn.send_size(rect[2], rect[3])
                runtime.conn.sock.setblocking(False)
            try:
                result = self._loop(out, stdin_fd)
            except OSError as error:  # EIO writing to a dead terminal
                outtrace.dump("terminal write failed: %s" % error)
                raise
            self.last_active_pane_id = self.active_id
            return result
        finally:
            self._restore_ime()
            self.last_active_pane_id = self.active_id
            keep = result is not None and result.action != "detach"
            for runtime in self.runtimes:
                if not runtime.conn:
                    continue
                if keep and not (result.action == "dead"
                                 and result.arg[0] == runtime.id):
                    PANE_CACHE.park(runtime)
                else:
                    runtime.conn.close()
            outtrace.note("result", result.action if result else "error")
            try:
                if result is None or result.action not in self.SEAMLESS:
                    out.write(self.LEAVE)
                    Client._term_active = False
                out.flush()
            except OSError:
                Client._term_active = False
            signal.signal(signal.SIGWINCH, old_handler)
            termios.tcsetattr(stdin_fd, termios.TCSANOW, old)

    _colors_probed = False  # class-wide: once per crow process
    # Results that re-attach right away without printing anything: the
    # outer terminal stays in the alternate screen with crow's modes on
    # (toggling it per window switch is churn Terminal.app doesn't need).
    SEAMLESS = ("switch", "switch-session", "split-v", "split-h", "zoom",
                "new", "open-file", "ssh-connect", "host-disconnect",
                "new-session", "rename-window", "rename-session",
                "kill-pane", "kill-session", "kill-window", "move-pane")
    _term_active = False
    LEAVE = (b"\x1b[r\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
             b"\x1b[?2004l\x1b[?1l\x1b[?25h\x1b[?1049l")

    @classmethod
    def leave_terminal(cls):
        """Undo the alternate screen + modes if a seamless switch left
        them on and no attach followed (error between runs)."""
        if cls._term_active:
            cls._term_active = False
            sys.stdout.buffer.write(cls.LEAVE)
            sys.stdout.buffer.flush()

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
            action = ("current-session", session["name"]) \
                if session.get("current") else \
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
        lines.append((" + 새 세션", "38;5;31;" + bg, ("new-session", None)))
        lines.append(("", plain, None))
        lines.append((" 호스트 · 리버스", heading, None))
        for host in data.get("hosts", []):
            state, color = self._host_state(host)
            failed = state.startswith("연결 실패")
            style = tone["31"] + ";" + bg if failed \
                else tone.get(color, color) + ";" + bg
            lines.append((" ✗ " + host.get("name", "?") if failed
                          else " ● " + host.get("name", "?"),
                          "1;" + style if failed else style,
                          ("reverse-host", host)))
            lines.append(("   " + state, style, ("reverse-host", host)))
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
        items = []
        for _text, _style, action in lines:
            if action and (not items or self._action_key(items[-1])
                           != self._action_key(action)):
                items.append(action)
        self._sidebar_items = items
        picked = None
        if self.sidebar_focus and items:
            keys = [self._action_key(a) for a in items]
            if self._sidebar_pick in keys:
                self._sidebar_sel = keys.index(self._sidebar_pick)
            self._sidebar_sel = max(0, min(self._sidebar_sel,
                                           len(items) - 1))
            picked = keys[self._sidebar_sel]
            self._sidebar_pick = picked
        out = []
        self._sidebar_rows = []
        for row in range(area_h):
            text, style, action = lines[row] if row < len(lines) \
                else ("", plain, None)
            if picked is not None and action and \
                    self._action_key(action) == picked:
                style = "1;38;5;231;48;5;31"
            fitted, _used = self._fit(text, content)
            out.append(("\x1b[%d;1H\x1b[%sm" % (row + 1, style)).encode()
                       + fitted.encode()
                       + b"\x1b[38;5;250m" + "│".encode() + b"\x1b[0m")
            if action:
                self._sidebar_rows.append((row, action))
        return b"".join(out)

    def _sidebar_click(self, gy):
        for row, action in self._sidebar_rows:
            if row == gy - 1:
                return self._sidebar_activate(action)
        return None

    def _sidebar_menu(self, gx, gy):
        """Right-click in the sidebar: session menu, or a host's menu."""
        for row, action in self._sidebar_rows:
            if row != gy - 1:
                continue
            kind, arg = action
            if kind in ("switch-session", "current-session"):
                self._session_menu(arg, kind == "current-session", gx, gy)
            elif kind == "reverse-host":
                self._open_reverse_for_host(arg)
            return

    def _sidebar_row_of(self, action):
        key = self._action_key(action)
        for row, item in self._sidebar_rows:
            if self._action_key(item) == key:
                return row + 1
        return 1

    def _sidebar_activate(self, action):
        kind, arg = action
        if kind == "switch-session":
            return AttachResult("switch-session", arg)
        if kind == "new-session":
            self.sidebar_focus = False
            return AttachResult("new-session", self._active_cwd())
        if kind == "reverse-host":
            self._open_reverse_for_host(arg)
        elif kind == "add-host":
            self._open_host_form()
        elif kind in ("key", "keys"):
            self._open_keys(arg)
        elif kind == "key-generate":
            return AttachResult("key-generate", None)
        return None

    @staticmethod
    def _action_key(action):
        kind, arg = action
        if isinstance(arg, dict):
            arg = arg.get("name") or arg.get("path")
        return kind, arg

    # Keyboard focus on the left sidebar (C-b h). Panels and the host
    # form opened from it take the keys first; closing them lands back
    # here.
    sidebar_focus = False
    _sidebar_sel = 0
    _sidebar_pick = None
    _sidebar_items = ()

    def _focus_sidebar(self):
        if self.explorer_open:
            self.explorer_open = False
        Client.sidebar_open = True
        self.sidebar_focus = True
        self._sidebar_pick = None
        hosts = self._sidebar_data.get("hosts") or []
        self._sidebar_sel = 0
        if hosts:
            self._sidebar_pick = ("reverse-host", hosts[0].get("name"))
        else:
            self._sidebar_pick = ("add-host", None)
        if self._ime_saved is None:
            self._ime_saved = imswitch.to_latin()
        self._focus_dirty = True

    def _leave_sidebar(self):
        self.sidebar_focus = False
        self._focus_dirty = True

    def _sidebar_key(self, unit):
        items = list(self._sidebar_items)
        if not Client.sidebar_open or self.explorer_open:
            self._leave_sidebar()
            return None
        self._focus_dirty = True
        key = None if unit.startswith(b"\x1b") and len(unit) > 1 else \
            latin_key(unit.decode(errors="ignore"))
        final = unit[-1:] if unit.startswith(b"\x1b[") or \
            unit.startswith(b"\x1bO") else b""
        step = -1 if final == b"A" or key == "k" else \
            1 if final == b"B" or key == "j" else 0
        if step and items:
            self._sidebar_sel = (self._sidebar_sel + step) % len(items)
            self._sidebar_pick = self._action_key(items[self._sidebar_sel])
            return None
        if key in ("\x1b", "q") or final == b"D" or key == "h":
            self._leave_sidebar()
            return None
        if not items:
            return None
        action = items[min(self._sidebar_sel, len(items) - 1)]
        if key in ("\r", "\n", "l", " ") or final == b"C":
            return self._sidebar_activate(action)
        if action[0] in ("switch-session", "current-session"):
            self._menu_at = (3, self._sidebar_row_of(action))
            if key in ("d", "x"):
                self._confirm_kill_session(action[1])
                return None
            if key in ("e", "r"):
                self._rename_session_prompt(action[1])
                return None
        if key == "a":
            self._open_host_form()
        elif action[0] == "reverse-host" and key == "e":
            self._open_host_form(action[1])
        elif action[0] == "reverse-host" and key in ("d", "x"):
            self._open_reverse_for_host(action[1])
            self.reverse_stage = "host-delete"
        elif action[0] == "reverse-host" and key == "s":
            return AttachResult("ssh-connect", action[1])
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
        if self.sidebar_focus and not self.reverse_open and self.form is None:
            right = " ↑↓ 이동 · Enter 열기 · a 추가 · e 수정 · d 삭제 · " \
                "s SSH · Esc 나가기 "
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
        if self.menu is not None:
            chunks.append(self._menu_chunk())
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
        tabs = self._window_tabs()
        tabs_w = sum(self._width(label) for label, _ in tabs) + \
            (3 if self.windows_provider else 0) + 1
        if files_w + self._width(left) + tabs_w + self._width(right) > cols:
            # Window tabs matter more than the key hints.
            right = " %s " % time.strftime("%H:%M")
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
        for index, (label, current) in enumerate(tabs):
            label_w = self._width(label)
            if col + label_w > hi:
                break
            body.append(self.TAB_CURRENT_STYLE if current else self.TAB_STYLE)
            body.append(label.encode(errors="replace"))
            buttons.append((col + 1, col + 1 + label_w, ("window", index)))
            col += label_w
        if self.windows_provider and col + 3 <= hi:  # new window
            body.append(self.TAB_STYLE + b" + ")
            buttons.append((col + 1, col + 4, "new-window"))
            col += 3
        body.append(self.BAR_STYLE)
        if col + right_w <= cols:
            body.append(b" " * (cols - col - right_w)
                        + right.encode(errors="replace"))
            start = cols - right_w + 1  # 1-based column of the hints
            for word, action in (("C-b d detach", "detach"),
                                 ("C-b ? help", "help")):
                if word in right:
                    x0 = start + self._width(right[:right.index(word)])
                    buttons.append((x0, x0 + self._width(word), action))
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
            return b"\x1b[1;1H\x1b[?25l"
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
            return b"".join(chunks)
        # Even hidden, the real cursor must sit somewhere sane: the IME
        # draws its composition there, and Terminal.app crashes drawing
        # it at the bar's last column (pending wrap) or mid-wide-char.
        chunks.append(self._park_cursor(rect, screen))
        if rect and screen.cursor_visible and active.view_offset == 0 \
                and not ((self.explorer_open and self.explorer_focus)
                        or self.reverse_open or self.help_open
                        or self.sidebar_focus or self.form is not None
                        or self.menu is not None):
            chunks.append(b"\x1b[?25h")
        else:
            chunks.append(b"\x1b[?25l")
        return b"".join(chunks)

    @staticmethod
    def _park_cursor(rect, screen):
        if not rect:
            return b"\x1b[1;1H"
        y = max(0, min(screen.cy, rect[3] - 1, screen.rows - 1))
        x = max(0, min(screen.cx, rect[2] - 1, screen.cols - 1))
        line = screen.grid[y]
        while x > 0 and line[x][0] == "":  # right half of a wide char
            x -= 1
        return ("\x1b[%d;%dH" % (rect[1] + y + 1, rect[0] + x + 1)).encode()

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
    # A pane still blank this long after attach gets its PTY shrunk by a
    # row and restored: a real size change makes the app repaint. A
    # same-size SIGWINCH is not enough (Node TUIs like claude ignore it)
    # and a replay cut mid-stream can leave nothing drawn.
    NUDGE_DELAY = 0.6
    NUDGE_HOLD = 0.15

    def _nudge_tick(self, rects, now):
        """Called every loop turn; runs the blank-pane nudge once."""
        if self._nudge_at is not None and now >= self._nudge_at:
            self._nudge_at = None
            for runtime in self.runtimes:
                rect = rects.get(runtime.id)
                if not rect or rect[3] < 3 or runtime.conn is None or \
                        not runtime.screen.is_blank():
                    continue
                try:
                    runtime.conn.send_size(rect[2], rect[3] - 1)
                except socket.error:
                    continue
                self._nudged.append(runtime)
            if self._nudged:
                self._unnudge_at = now + self.NUDGE_HOLD
        if self._unnudge_at is not None and now >= self._unnudge_at:
            self._unnudge_at = None
            for runtime in self._nudged:
                rect = rects.get(runtime.id)
                if rect:
                    try:
                        runtime.conn.send_size(rect[2], rect[3])
                    except socket.error:
                        pass
            self._nudged = []

    _nudge_at = None
    _unnudge_at = None

    def _loop(self, out, stdin_fd):
        rects = self._rects()
        self._nudge_at = time.time() + self.NUDGE_DELAY
        self._unnudge_at = None
        self._nudged = []
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
            self._nudge_tick(rects, time.time())
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
                fed, code = runtime.consume()
                changed = changed or fed
                if code is not None:
                    return AttachResult("dead", (runtime.id, code))
            if changed:
                paint_due = True
            if paint_due and time.time() - self._last_draw >= 0.016:
                self._render(out, rects)
                paint_due = False
            if stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 65536)
                except OSError:  # EIO: the terminal went away
                    data = b""
                if not data:
                    # Terminal window/app gone (crash or close): keep the
                    # flight recorder before anything else can fail.
                    outtrace.dump("terminal input closed")
                    return AttachResult("detach")
                outtrace.typed(data)
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
                or self.form is not None or self.sidebar_focus
                or (self.explorer_open and self.explorer_focus)):
            self._restore_ime()
        return result

    def _unit_inner(self, unit, rects):
        if self.form is not None:
            self._focus_dirty = True
            return self._form_key(unit)
        if self.menu is not None:
            return self._menu_key(unit)
        if self._swap_from is not None and not MOUSE_RE.match(unit):
            self._end_swap()
            self._focus_dirty = True
            if unit == b"\x1b":
                return None
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
        if self.sidebar_focus:
            return self._sidebar_key(unit)
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

    _drag = None  # (node, split, strip, origin, span) while resizing
    _swap_from = None  # pane waiting for the click that picks its partner

    def _start_swap(self, pane_id):
        self._swap_from = pane_id
        self._flash("자리를 바꿀 pane을 클릭하세요 · Esc 취소", seconds=30)
        self._focus_dirty = True

    def _end_swap(self):
        self._swap_from = None
        self._paste_msg = None
        self._probe_dirty = True

    def _swap_panes(self, a, b):
        """Exchange two panes' places in the layout (sizes stay with the
        places) and persist it."""
        if a == b or self._window is None:
            return False

        def walk(tree):
            if not tree:
                return
            if "leaf" in tree:
                if tree["leaf"] == a:
                    tree["leaf"] = b
                elif tree["leaf"] == b:
                    tree["leaf"] = a
                return
            walk(tree.get("a"))
            walk(tree.get("b"))
        walk(self._window.get("layout"))
        self._focus_dirty = True
        if self.layout_saver:
            try:
                self.layout_saver(self._window["id"],
                                  self._window.get("layout"))
            except Exception:
                pass
        return True

    def _swap_step(self, step):
        """C-b { / }: swap the active pane with the previous / next one."""
        if self._window is None or self._window.get("zoomed"):
            return
        order = registry_mod.layout_pane_ids(self._window.get("layout"))
        if len(order) < 2 or self.active_id not in order:
            return
        other = order[(order.index(self.active_id) + step) % len(order)]
        self._swap_panes(self.active_id, other)

    def _divider_at(self, gx, gy):
        if self._window is None or self._window.get("zoomed"):
            return None
        col, row = gx - 1, gy - 1
        for found in split_dividers(self._window.get("layout"),
                                    *self._area()):
            x, y, w, h = found[2]
            if x <= col < x + w and y <= row < y + h:
                return found
        return None

    def _drag_divider(self, button, gx, gy, final):
        node, split, _strip, origin, span = self._drag
        if final == b"m" or not button & 32:  # released (or a new press)
            self._drag = None
            if self.layout_saver and self._window is not None:
                try:
                    self.layout_saver(self._window["id"],
                                      self._window.get("layout"))
                except Exception:
                    pass
            return
        ratio = drag_ratio((gx if split == "v" else gy) - 1, origin, span)
        if ratio != node.get("ratio"):
            node["ratio"] = ratio
            self._focus_dirty = True  # loop re-lays out and resizes PTYs

    def _mouse(self, unit, match, rects):
        button, gx, gy = int(match.group(1)), int(match.group(2)), \
            int(match.group(3))
        final = match.group(4)
        if self._drag is not None:
            self._drag_divider(button, gx, gy, final)
            return
        if self._swap_from is not None:
            if final != b"M" or button & 96:
                return  # releases, drags and the wheel
            source = self._swap_from
            self._end_swap()
            for pane_id, (x, y, w, h) in rects.items():
                if x < gx <= x + w and y < gy <= y + h:
                    if self._swap_panes(source, pane_id):
                        self._focus(source)  # focus stays on the mover
                    break
            self._focus_dirty = True
            return
        _, rows = self._winsize()
        if gy >= rows:  # status bar
            if final == b"M" and button & ~24 == 2:
                for x0, x1, action in self._bar_buttons:
                    if x0 <= gx < x1 and action[0] == "window":
                        self._window_menu(action[1], gx, gy)
                return
            if final == b"M":
                for x0, x1, action in self._bar_buttons:
                    if not x0 <= gx < x1:
                        continue
                    if action == "explorer":
                        self._toggle_explorer()
                    elif action == "sidebar":
                        self._toggle_sidebar()
                    elif action == "new-window":
                        self._explorer_result = AttachResult("new")
                    elif action == "detach":
                        self._explorer_result = AttachResult("detach")
                    elif action == "help":
                        self.help_open = True
                        self._help_opened_at = time.time()
                        self._help_full = False
                        self._focus_dirty = True
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
            if final == b"M" and button & ~24 == 2:
                self._sidebar_menu(gx, gy)
                self._focus_dirty = True
                return
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
        if self.sidebar_focus and final == b"M" and button not in (64, 65):
            self._leave_sidebar()
        if final == b"M" and button == 0:
            divider = self._divider_at(gx, gy)
            if divider is not None:  # grab a split border to resize
                self._drag = divider
                return
        for pane_id, (x, y, w, h) in rects.items():
            if not (x < gx <= x + w and y < gy <= y + h):
                continue
            runtime = self._runtime(pane_id)
            if runtime is None:
                return
            if final == b"M" and button & ~24 == 2:
                # Right-click: the pane menu (Shift+right-click still
                # reaches an app that uses the mouse).
                self._focus(pane_id)
                self._focus_dirty = True
                self._pane_menu(pane_id, gx, gy)
                return
            if final == b"M" and button == 0 and \
                    not runtime.screen.mouse_mode:
                choice = self._prompt_choice(runtime, gx - x - 1, gy - y - 1)
                if choice is not None:  # a clickable crow prompt option
                    self._focus(pane_id)
                    self._focus_dirty = True
                    runtime.view_offset = 0
                    try:
                        runtime.conn.send("D", choice)
                    except (socket.error, AttributeError):
                        pass
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

    # Prompts crow itself prints inside a pane, with clickable options:
    # (marker on the clicked row or the one above, [(label, input)]).
    CLICK_PROMPTS = (
        ("[crow-cli] ssh 연결이 끊겼습니다",
         (("Enter: 다시 접속", b"\r"), ("q: 연결 끊기", b"q\r"),
          ("s: 로컬 셸", b"s\r"))),
    )

    def _prompt_choice(self, runtime, col, row):
        """Input for a crow prompt option at a pane cell (0-based)."""
        lines = runtime.screen.lines_for_view(runtime.view_offset)

        def text_of(index):
            chars, cols = [], []
            for x, cell in enumerate(lines[index]):
                if cell[0]:  # "" = right half of a wide character
                    chars.append(cell[0])
                    cols.append(x)
            return "".join(chars), cols
        if not 0 <= row < len(lines):
            return None
        text, cols = text_of(row)
        near = text + (text_of(row - 1)[0] if row else "")
        for marker, options in self.CLICK_PROMPTS:
            if marker not in near:
                continue
            for label, data in options:
                at = text.find(label)
                if at < 0:
                    continue
                last = at + len(label) - 1
                if cols[at] <= col < cols[last] + char_width(text[last]):
                    return data
        return None

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
            connected = bool(host.get("ssh_session"))
            ssh_rows = [
                {"kind": "ssh", "label": "SSH 세션으로 이동"},
                {"kind": "disconnect", "label": "SSH 연결 끊기 (세션 닫기)"},
            ] if connected else [
                {"kind": "ssh", "label": "SSH 접속 (터미널 열기)"}]
            return ssh_rows + [
                {"kind": "reverse-open",
                 "label": "리버스 에이전트 실행 →"},
                {"kind": "check", "label": "키·연결 확인" + suffix},
                {"kind": "install", "label": "공개키 서버에 설치"},
                {"kind": "info", "label": ""},
                {"kind": "host-edit", "label": "호스트 수정…"},
                {"kind": "host-delete", "label": "호스트 삭제…"},
            ]
        if self.reverse_stage == "host-delete" and self._pending_host:
            return [{"kind": "host-delete-yes",
                     "label": "삭제 (열린 세션은 그대로)"},
                    {"kind": "host-delete-no", "label": "취소"}]
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

    def _active_cwd(self):
        root = None
        if self.explorer_root_provider:
            try:
                root = self.explorer_root_provider(self.active_id)
            except Exception:
                root = None
        if not root:
            active = self._active()
            root = active.pane.get("cwd") if active else os.getcwd()
        return root

    def _reverse_activate(self):
        rows = self._reverse_rows()
        if not rows or self.reverse_sel >= len(rows):
            return None
        row = rows[self.reverse_sel]
        if self.reverse_stage in self.KEY_STAGES:
            result = self._key_activate(row)
            self._focus_dirty = True
            return result
        if row["kind"] in ("none", "info"):
            pass
        elif row["kind"] == "host-edit":
            self._open_host_form(self._pending_host)
        elif row["kind"] == "host-delete":
            self.reverse_stage = "host-delete"
            self.reverse_sel = 0
        elif row["kind"] == "host-delete-no":
            self.reverse_stage = "menu"
            self.reverse_sel = 0
        elif row["kind"] == "host-delete-yes":
            if self.host_form and "delete" in self.host_form:
                self._flash(self.host_form["delete"](self._pending_host))
            self._pending_host = None
            self.reverse_open = False
            self._sidebar_at = 0.0
        elif row["kind"] == "ssh":
            self.reverse_open = False
            return AttachResult("ssh-connect", self._pending_host)
        elif row["kind"] == "disconnect":
            self.reverse_open = False
            self._sidebar_at = 0.0
            return AttachResult("host-disconnect", self._pending_host)
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
            root = self._active_cwd()
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
            self._reverse_back()
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

    _reverse_back_hit = (0, 0)

    def _reverse_can_go_back(self):
        stage = self.reverse_stage
        return stage in ("key", "key-host", "key-delete", "host-delete") \
            or (stage == "provider" and bool(self._pending_host))

    def _reverse_back_label(self):
        return " ‹ 뒤로 " if self._reverse_can_go_back() else " ✕ 닫기 "

    def _reverse_back(self):
        """One stage back (q / Esc / the title's ‹ 뒤로), else close."""
        if self.reverse_stage in self.KEY_STAGES:
            self._key_back()
        elif self.reverse_stage == "host-delete" or \
                (self.reverse_stage == "provider" and self._pending_host):
            self.reverse_stage = "menu"  # back to the host menu
            self.reverse_sel = 0
        else:
            self.reverse_open = False
        self._focus_dirty = True

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
        x0, x1 = self._reverse_back_hit
        if gy == 1 and x0 <= gx < x1:
            self._reverse_back()
            return
        row = gy - 3  # title + subtitle rows
        if row < 0:
            return
        index = self.reverse_scroll + row
        if index >= len(self._reverse_rows()):
            return
        self.reverse_sel = index  # one click runs it, like the sidebar
        self._focus_dirty = True
        result = self._reverse_activate()
        if result is not None:
            self._explorer_result = result

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
            if self.reverse_stage in ("menu", "host-delete") \
            else " 리버스 에이전트 "
        back = self._reverse_back_label()
        fill = max(0, inner - self._width(title) - self._width(back))
        out.append(("\x1b[1;%dH" % col).encode() + edge + "┌".encode()
                   + bg + b"\x1b[1;38;5;31m" + title.encode() + edge
                   + ("─" * fill).encode() + bg + b"\x1b[38;5;31m"
                   + back.encode() + edge + "┐".encode() + b"\x1b[0m")
        self._reverse_back_hit = (col + 1 + inner - self._width(back),
                                  col + 1 + inner)
        key_name = (self._pending_key or {}).get("name", "")
        if self.reverse_stage == "keys":
            subtitle = " 관리 키 + ~/.ssh 자동 발견"
        elif self.reverse_stage == "key":
            subtitle = " " + key_name
        elif self.reverse_stage == "key-host":
            subtitle = " %s 공개키를 설치할 호스트" % key_name
        elif self.reverse_stage == "key-delete":
            subtitle = " %s 삭제" % key_name
        elif self.reverse_stage == "host-delete" and self._pending_host:
            subtitle = " %s 호스트를 삭제할까요?" % \
                self._pending_host.get("name", "?")
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
        x0, x1 = self._explorer_refresh_hit
        if gy == 1 and x0 <= gx < x1:
            self._explorer_load()
            self._focus_dirty = True
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
        # Folders open on one click; files (which open an editor pane)
        # still take a double click.
        if self.explorer_entries[index][1] or \
                (last_index == index and now - last_time < 0.4):
            result = self._explorer_open_entry()
            if result is not None:
                self._explorer_result = result
        self._last_click = (now, index)

    _explorer_result = None
    _explorer_refresh_hit = (0, 0)

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
        rows.append(("  마우스: pane 우클릭 = 분할·확대·닫기·이름 메뉴 · "
                     "경계선 드래그 = 크기 조절", "38;5;245"))
        rows.append(("  하단 탭 클릭 = 윈도우 전환 · + = 새 윈도우 · "
                     "detach / help 도 클릭", "38;5;245"))
        rows.append(("  왼쪽 탭: 세션·호스트 클릭 = 이동 · 세션 우클릭 = "
                     "이름 바꾸기·삭제", "38;5;245"))
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
    FORM_FIELDS = ("address", "name", "port", "key", "save", "cancel")
    FORM_TEXT = ("address", "name", "port")

    def _open_host_form(self, host=None):
        """Empty form adds a host; passing a saved host edits it."""
        keys = [("기본 키 자동 탐색", None)]
        if self.host_form:
            keys += list(self.host_form["keys"]())
        host = host or {}
        key = 0
        identity = host.get("identity")
        if identity:
            wanted = os.path.expanduser(identity)
            key = next((i for i, (_label, path) in enumerate(keys)
                        if path and os.path.expanduser(path) == wanted), None)
            if key is None:
                keys.append((os.path.basename(identity), identity))
                key = len(keys) - 1
        address = host.get("address", "")
        port = str(host["port"]) if host.get("port") else ""
        name = host.get("name", "")
        self.form = {"address": address, "port": port, "name": name,
                     "caret": {"address": len(address), "port": len(port),
                               "name": len(name)},
                     "view": {}, "keys": keys, "key": key,
                     "edit": host.get("name"),
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
        extra = {"original": form["edit"]} if form.get("edit") else {}
        if form["name"].strip():
            extra["name"] = form["name"].strip()
        try:
            record = self.host_form["save"](address, form["port"].strip(),
                                            identity, **extra)
        except ValueError as exc:
            form["error"] = str(exc)
            return None
        self._close_host_form()
        self._sidebar_at = 0.0
        self._open_reverse_for_host(record)
        return None

    def _form_key(self, unit):
        form = self.form
        if form.get("kind") == "folder":
            return self._folder_key(unit)
        if form.get("kind") == "prompt":
            return self._prompt_key(unit)
        mouse = MOUSE_RE.match(unit)
        if mouse:
            if mouse.group(4) == b"M" and not int(mouse.group(1)) & 96:
                gx, gy = int(mouse.group(2)), int(mouse.group(3))
                for row, x0, x1, field in form["hits"]:
                    if gy == row and x0 <= gx < x1:
                        form["focus"] = self.FORM_FIELDS.index(field)
                        if field in form["caret"]:
                            form["caret"][field] = self._form_click_caret(
                                form, field, gx)
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
        caret = form["caret"]
        if unit in (b"\x1b[C", b"\x1b[D"):
            step = 1 if unit == b"\x1b[C" else -1
            if field == "key":
                form["key"] = (form["key"] + step) % len(form["keys"])
            elif field in ("save", "cancel"):
                form["focus"] = self.FORM_FIELDS.index(
                    "save" if field == "cancel" else "cancel")
            else:
                caret[field] = max(0, min(len(form[field]),
                                          caret[field] + step))
            return None
        if field not in self.FORM_TEXT:
            return None
        value, at = form[field], min(caret[field], len(form[field]))
        if unit in (b"\x1b[H", b"\x1bOH", b"\x1b[1~", b"\x01"):
            caret[field] = 0
            return None
        if unit in (b"\x1b[F", b"\x1bOF", b"\x1b[4~", b"\x05"):
            caret[field] = len(value)
            return None
        if unit in (b"\x7f", b"\x08"):
            if at:
                form[field] = value[:at - 1] + value[at:]
                caret[field] = at - 1
            form["error"] = None
            return None
        if unit in (b"\x1b[3~", b"\x04"):
            form[field] = value[:at] + value[at + 1:]
            form["error"] = None
            return None
        if unit == b"\x15":  # ^U: clear before the caret
            form[field] = value[at:]
            caret[field] = 0
            return None
        if unit.startswith(b"\x1b"):
            return None  # bracketed-paste markers and other sequences
        text = unit.decode(errors="ignore")
        if field == "port":
            text = "".join(c for c in text if c.isdigit())
        elif field == "name":
            text = "".join(c for c in text if not c.isspace())
        elif text.isspace():
            text = ""
        if text and text.isprintable():
            form[field] = value[:at] + text + value[at:]
            caret[field] = at + len(text)
            form["error"] = None
        return None

    def _form_click_caret(self, form, field, gx):
        """Caret index for a click at 1-based column gx in a text field."""
        col0, start = form["view"].get(field, (gx, 0))
        value, x, index = form[field], col0, start
        while index < len(value):
            w = self._width(value[index])
            if x + w > gx:
                break
            x += w
            index += 1
        return index

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
            limit = field_w - 2
            if name in form["caret"]:
                value = form[name]
                caret = min(form["caret"][name], len(value))
                start = min(form["view"].get(name, (0, 0))[1], caret)
                while self._width(value[start:caret]) > limit:
                    start += 1  # scroll so the caret stays visible
                form["view"][name] = (col + 1, start)
                if not empty:
                    shown = value[start:]
                if on:
                    form["cursor"] = (row, col + 1 + (0 if empty else
                                      self._width(value[start:caret])))
            while self._width(shown) > limit and len(shown) > 1:
                shown = shown[:-1]
            lines.append(body + "  "
                         + ("\x1b[1;48;5;235;38;5;51m" if on else body)
                         + self._pad(label, label_w) + text_style + " "
                         + self._pad(shown, field_w - 1) + body
                         + " " * (inner - 2 - label_w - field_w))

        address = form["address"].strip()
        derived = form.get("edit") or address and self.host_form and \
            self.host_form["derive"](address)
        key_label, key_path = form["keys"][form["key"]]
        name_hint = "%s (주소에서 자동)" % derived if derived else \
            "비우면 주소에서 자동"
        plain("")
        plain("  열린 세션은 그대로, 다음 접속부터 적용돼요" if form.get("edit")
              else "  저장하면 왼쪽 탭에서 바로 접속 · 리버스 할 수 있어요", dim)
        plain("")
        field("SSH 주소", "address", form["address"] or "user@host 또는 host",
              not form["address"])
        field("별칭", "name", form["name"] or name_hint, not form["name"])
        field("포트", "port", form["port"] or "22", not form["port"])
        field("키", "key", "‹ %s ›" % self._pad(key_label, field_w - 6).rstrip(),
              False)
        plain("")
        plain("  키 파일: %s" % key_path if key_path else
              "  키 파일: ~/.ssh/id_* 중 ssh가 고름", dim)
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
        cancel = ("Esc 취소", "cancel")
        if form.get("kind") == "folder":
            return self._popup_chunk(
                form, "─ 리버스 작업 폴더 ",
                "Tab 완성 · ↑↓ 고르기 · Enter 변경 · Esc 취소 ",
                (("Enter 변경", "save"), cancel), 64, 16,
                self._folder_lines)
        if form.get("kind") == "prompt":
            return self._popup_chunk(
                form, "─ %s " % form["title"], "Enter 확인 · Esc 취소 ",
                (("Enter 확인", "save"), cancel), 54, 8, self._prompt_lines)
        return self._popup_chunk(
            form, "─ 호스트 수정 " if form.get("edit") else "─ 호스트 추가 ",
            "Tab 이동 · ←→ 키 선택 · Enter 저장 · Esc 취소 ",
            (("Enter 저장", "save"), cancel), 60, 14, self._form_lines)

    def _popup_chunk(self, form, title, hint, hint_hits, full_w, full_h,
                     lines_fn):
        """A centered popup box that pops in, filled by lines_fn once the
        animation ends; the hint words in the bottom edge are clickable."""
        cols, rows = self._winsize()
        full_w = min(full_w, cols - 2)
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

        put(0, box + "╭" + self._pad(title, inner, "─") + "╮")
        blank = body + " " * inner
        lines = [blank] * (height - 2)
        if t >= 1.0:
            lines = lines_fn(form, top, left, inner, body, dim)
            form["full"] = True
        for i, text in enumerate(lines[:height - 2]):
            put(i + 1, box + "│" + text + box + "│")
        if self._width(hint) + 2 > inner:
            hint = ""
        dashes = "─" * (inner - self._width(hint) - (1 if hint else 0))
        put(height - 1, box + "╰" + dashes + (" " if hint else "")
            + "\x1b[38;5;245m" + hint + box + "╯")
        form["box"] = (top + 1, left + 1, top + height, left + width)
        if hint and t >= 1.0:
            start = left + 3 + len(dashes)  # 1-based column of the hint
            for word, name in hint_hits:
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

    # -- context menu -----------------------------------------------------------
    # A small popup list at a screen cell: right-click menus (pane,
    # session) and confirmations. Items are dicts {"label", "do",
    # "danger"}; do() returns an AttachResult or None and may open the
    # next popup (the menu is already closed when it runs). MENU_SEP
    # draws a rule.
    MENU_SEP = {"label": "", "do": None, "sep": True}
    menu = None

    def _open_menu(self, items, gx, gy, title=None, confirm=False):
        selectable = [i for i, item in enumerate(items)
                      if item.get("do") and not item.get("sep")]
        self.menu = {"items": items, "x": gx, "y": gy, "title": title,
                     "sel": selectable[0] if selectable else 0,
                     "confirm": confirm, "hits": [], "box": None}
        self.help_open = False
        self.reverse_open = False
        self._focus_dirty = True

    def _menu_confirm(self, title, label, do):
        """Replace the menu with a yes/cancel pair at the same place."""
        x, y = self._menu_at
        self._open_menu([{"label": label, "do": do, "danger": True},
                         {"label": "취소", "do": lambda: None}],
                        x, y, title=title, confirm=True)

    _menu_at = (1, 1)

    def _menu_activate(self, index):
        menu = self.menu
        item = menu["items"][index] if 0 <= index < len(menu["items"]) \
            else None
        if not item or not item.get("do") or item.get("sep"):
            return None
        self._menu_at = (menu["x"], menu["y"])
        self.menu = None
        self._focus_dirty = True
        return item["do"]()

    def _menu_move(self, step):
        menu = self.menu
        count = len(menu["items"])
        index = menu["sel"]
        for _ in range(count):
            index = (index + step) % count
            item = menu["items"][index]
            if item.get("do") and not item.get("sep"):
                menu["sel"] = index
                return

    def _menu_key(self, unit):
        menu = self.menu
        mouse = MOUSE_RE.match(unit)
        if mouse:
            button = int(mouse.group(1))
            if mouse.group(4) != b"M" or button & 96:
                return None  # releases, drags and the wheel
            gx, gy = int(mouse.group(2)), int(mouse.group(3))
            for row, x0, x1, index in menu["hits"]:
                if gy == row and x0 <= gx < x1:
                    return self._menu_activate(index)
            y0, x0, y1, x1 = menu["box"] or (0, 0, -1, -1)
            if not (y0 <= gy <= y1 and x0 <= gx <= x1):
                self.menu = None  # click outside = cancel
                self._focus_dirty = True
            return None
        key = latin_key(unit.decode(errors="ignore"))
        if unit == b"\x1b" or key == "q" or (menu["confirm"] and key == "n"):
            self.menu = None
            self._focus_dirty = True
        elif unit in (b"\x1b[A", b"\x1b[Z") or key == "k":
            self._menu_move(-1)
        elif unit in (b"\x1b[B", b"\t") or key == "j":
            self._menu_move(1)
        elif menu["confirm"] and key == "y":
            return self._menu_activate(0)
        elif key in ("\r", "\n", " "):
            return self._menu_activate(menu["sel"])
        return None

    def _menu_chunk(self):
        menu = self.menu
        cols, rows = self._winsize()
        labels = [item["label"] for item in menu["items"]]
        title = " %s " % menu["title"] if menu["title"] else ""
        inner = max([self._width(l) + 4 for l in labels]
                    + [self._width(title) + 2, 16])
        inner = min(inner, cols - 2)
        width, height = inner + 2, len(labels) + 2
        left = max(0, min(menu["x"] - 1, cols - width))  # 0-based
        top = max(0, min(menu["y"] - 1, rows - 1 - height))
        box = "\x1b[0;48;5;235;38;5;51m"
        body = "\x1b[0;48;5;235;38;5;252m"
        out = []
        menu["hits"] = []

        def put(row, text):
            out.append("\x1b[%d;%dH" % (top + row + 1, left + 1) + text)

        put(0, box + "╭" + self._pad("─" + title if title else "", inner,
                                     "─") + "╮")
        for i, item in enumerate(menu["items"]):
            if item.get("sep"):
                put(i + 1, box + "├" + "─" * inner + "┤")
                continue
            on = i == menu["sel"]
            if on:
                style = "\x1b[0;1;48;5;24;38;5;231m"
            elif item.get("info"):
                style = "\x1b[0;48;5;235;38;5;117m"
            elif not item.get("do"):
                style = "\x1b[0;48;5;235;38;5;242m"
            elif item.get("danger"):
                style = "\x1b[0;48;5;235;38;5;203m"
            else:
                style = body
            put(i + 1, box + "│" + style
                + self._pad(("▸ " if on else "  ") + item["label"], inner)
                + box + "│")
            if item.get("do"):
                menu["hits"].append((top + i + 2, left + 2,
                                     left + 2 + inner, i))
        put(height - 1, box + "╰" + "─" * inner + "╯")
        menu["box"] = (top + 1, left + 1, top + height, left + width)
        return ("".join(out) + "\x1b[0m").encode(errors="replace")

    # -- menus built on it ------------------------------------------------------
    def _pane_menu(self, pane_id, gx, gy):
        """Right-click on a pane: everything C-b does to a pane."""
        runtime = self._runtime(pane_id)
        zoomed = bool(self._window and self._window.get("zoomed"))
        items = [
            {"label": "좌우로 분할",
             "do": lambda: AttachResult("split-v", pane_id)},
            {"label": "위아래로 분할",
             "do": lambda: AttachResult("split-h", pane_id)},
            {"label": "확대 해제" if zoomed else "확대",
             "do": lambda: AttachResult("zoom", pane_id)},
        ]
        if not zoomed and len(self.runtimes) > 1:
            items.append({"label": "자리 바꾸기…",
                          "do": lambda: self._start_swap(pane_id)})
        if self._move_targets(pane_id):
            items.append({"label": "다른 윈도우로 옮기기…",
                          "do": lambda: self._move_menu(pane_id)})
        root = self._reverse_root(runtime)
        if root:
            items[:0] = [{"label": "작업 폴더", "do": None, "info": True},
                         {"label": self._short_path(root, 40), "do": None,
                          "info": True},
                         self.MENU_SEP]
            items.append({"label": "작업 폴더 바꾸기…",
                          "do": lambda: self._open_folder_form(pane_id)})
        items += [self.MENU_SEP,
                  {"label": "새 윈도우", "do": lambda: AttachResult("new")},
                  {"label": "윈도우 이름 바꾸기…",
                   "do": self._rename_window_prompt},
                  self.MENU_SEP,
                  {"label": "pane 닫기…", "danger": True,
                   "do": lambda: self._confirm_kill_pane(pane_id)}]
        self._open_menu(items, gx, gy)

    def _move_targets(self, pane_id):
        """[(label, window index or None = new window)] a pane can move to."""
        tabs = self.windows_provider() if self.windows_provider else []
        targets = [("%d:%s" % (i, name), i)
                   for i, (name, current) in enumerate(tabs) if not current]
        if len(self.runtimes) > 1:
            targets.append(("+ 새 윈도우", None))
        return targets

    def _move_menu(self, pane_id):
        items = [{"label": label,
                  "do": (lambda index=index:
                         AttachResult("move-pane", (pane_id, index)))}
                 for label, index in self._move_targets(pane_id)]
        x, y = self._menu_at
        self._open_menu(items, x, y, title="어느 윈도우로?")

    def _short_path(self, path, limit):
        """~-abbreviated path, its head elided to fit `limit` cells."""
        text = self._tilde(path)
        if self._width(text) <= limit:
            return text
        kept = ""
        for char in reversed(text):
            if self._width(kept) + char_width(char) > limit - 1:
                break
            kept = char + kept
        return "…" + kept

    def _window_menu(self, index, gx, gy):
        """Right-click on a window tab: switch, rename, close."""
        tabs = self.windows_provider() if self.windows_provider else []
        if not 0 <= index < len(tabs):
            return
        name, current = tabs[index]
        items = []
        if not current:
            items.append({"label": "이 윈도우로 이동",
                          "do": lambda: AttachResult("switch",
                                                     ("index", index))})
        items += [{"label": "이름 바꾸기…",
                   "do": lambda: self._rename_window_prompt(index)},
                  self.MENU_SEP,
                  {"label": "윈도우 닫기…", "danger": True,
                   "do": lambda: self._menu_confirm(
                       "%d:%s 닫을까요?" % (index, name),
                       "닫기 (안의 pane 모두 종료)",
                       lambda: AttachResult("kill-window", index))}]
        self._open_menu(items, gx, gy, title="%d:%s" % (index, name))

    def _confirm_kill_pane(self, pane_id):
        runtime = self._runtime(pane_id)
        name = runtime.pane.get("name", "pane") if runtime else "pane"
        self._menu_confirm("%s 닫을까요?" % name, "닫기 (프로그램 종료)",
                           lambda: AttachResult("kill-pane", (pane_id, True)))

    def _session_menu(self, name, current, gx, gy):
        items = []
        if not current:
            items.append({"label": "이 세션으로 이동",
                          "do": lambda: AttachResult("switch-session",
                                                     name)})
        items += [{"label": "이름 바꾸기…",
                   "do": lambda: self._rename_session_prompt(name)},
                  self.MENU_SEP,
                  {"label": "세션 삭제…", "danger": True,
                   "do": lambda: self._confirm_kill_session(name)}]
        self._open_menu(items, gx, gy, title=name)

    def _confirm_kill_session(self, name):
        self._menu_confirm("%s 삭제할까요?" % name, "삭제 (실행 중인 것 모두 종료)",
                           lambda: AttachResult("kill-session", name))

    def _sessions_menu(self):
        """C-b s: pick a session from a popup list."""
        sessions = (self._sidebar_data or {}).get("sessions") or []
        if not sessions:
            return AttachResult("choose")
        items = [{"label": ("● " if s.get("current") else "  ") + s["name"],
                  "do": (lambda n=s["name"]:
                         AttachResult("switch-session", n))}
                 for s in sessions]
        cols, rows = self._winsize()
        self._open_menu(items, max(1, cols // 2 - 12),
                        max(1, (rows - len(items)) // 2), title="세션")
        current = [i for i, s in enumerate(sessions) if s.get("current")]
        if current:
            self.menu["sel"] = current[0]
        return None

    # -- one-line text prompt (rename) -------------------------------------------
    def _open_prompt(self, title, label, value, submit, note=""):
        """submit(text) returns an error string (prompt stays), or an
        AttachResult / None (prompt closes)."""
        self.form = {"kind": "prompt", "title": title, "label": label,
                     "value": value, "caret": {"value": len(value)},
                     "view": {}, "error": None, "note": note,
                     "submit": submit, "opened": time.time(),
                     "cursor": None, "hits": []}
        self.explorer_open = False
        self.reverse_open = False
        self.help_open = False
        self._focus_dirty = True

    def _prompt_submit(self):
        form = self.form
        result = form["submit"](form["value"].strip())
        if isinstance(result, str):
            form["error"] = result
            return None
        self._close_host_form()
        return result

    def _prompt_key(self, unit):
        form = self.form
        mouse = MOUSE_RE.match(unit)
        if mouse:
            if mouse.group(4) != b"M" or int(mouse.group(1)) & 96:
                return None
            gx, gy = int(mouse.group(2)), int(mouse.group(3))
            for row, x0, x1, target in form["hits"]:
                if gy == row and x0 <= gx < x1:
                    if target == "save":
                        return self._prompt_submit()
                    if target == "cancel":
                        self._close_host_form()
                    else:
                        form["caret"]["value"] = self._form_click_caret(
                            form, "value", gx)
                    return None
            y0, x0, y1, x1 = form.get("box") or (0, 0, 10 ** 6, 10 ** 6)
            if not (y0 <= gy <= y1 and x0 <= gx <= x1):
                self._close_host_form()
            return None
        if unit == b"\x1b":
            self._close_host_form()
            return None
        if unit in (b"\r", b"\n"):
            return self._prompt_submit()
        if self._text_edit(form, "value", unit):
            form["error"] = None
        return None

    def _prompt_lines(self, form, top, left, inner, body, dim):
        label_w = max(8, self._width(form["label"]) + 2)
        lines = [body + " " * inner]
        lines.append(self._text_field(
            form, "value", top + len(lines) + 2, left + 4 + label_w,
            inner - label_w - 6, form["label"], label_w, body, inner))
        lines.append(body + " " * inner)
        if form["error"]:
            lines.append("\x1b[0;48;5;235;38;5;203m"
                         + self._pad("  " + form["error"], inner))
        else:
            lines.append(dim + self._pad("  " + form["note"], inner))
        lines.append(body + " " * inner)
        lines.append(self._popup_buttons(form, top + len(lines) + 2, left,
                                         inner, body,
                                         (("save", "  확인  "),
                                          ("cancel", "  취소  "))))
        return lines

    def _popup_buttons(self, form, row, left, inner, body, buttons):
        """A centred row of buttons; records their click targets."""
        col = left + 1 + max(1, (inner - 24) // 2)  # 0-based
        text = body + " " * (col - left - 1)
        for name, label in buttons:
            form["hits"].append((row, col, col + 2 + self._width(label),
                                 name))
            text += "\x1b[0;48;5;244;38;5;231m" + label + body + "    "
            col += self._width(label) + 4
        return text + " " * max(0, inner - (col - left - 1))

    @staticmethod
    def _name_error(name):
        try:
            registry_mod.validate_name(name)
        except ValueError:
            return "글자·숫자·_ . - 만 쓸 수 있어요 (공백 없이, 64자까지)"
        return None

    def _rename_window_prompt(self, index=None):
        """Rename the shown window, or the window at tab `index`."""
        current = (self._window or {}).get("name", "")
        tabs = self.windows_provider() if self.windows_provider else []
        if index is not None and 0 <= index < len(tabs):
            current = tabs[index][0]

        def submit(name):
            if not name or name == current:
                return None
            return self._name_error(name) or AttachResult(
                "rename-window", name if index is None else (index, name))
        self._open_prompt("윈도우 이름", "이름", current, submit,
                          "하단 탭에 보이는 이름이에요")

    def _rename_session_prompt(self, old):
        taken = set(s["name"] for s in
                    (self._sidebar_data or {}).get("sessions") or [])

        def submit(name):
            if not name or name == old:
                return None
            if name in taken:
                return "이미 있는 세션 이름이에요"
            return self._name_error(name) or \
                AttachResult("rename-session", (old, name))
        self._open_prompt("세션 이름", "이름", old, submit,
                          "%s 세션의 새 이름" % old)

    # -- reverse folder popup -------------------------------------------------
    FOLDER_ROWS = 6

    @staticmethod
    def _reverse_root(runtime):
        """The client folder a reverse-agent pane works on, else None."""
        if runtime is None:
            return None
        return (runtime.pane.get("meta") or {}).get("client_root")

    @staticmethod
    def _tilde(path):
        home = os.path.expanduser("~")
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home):]
        return path

    def _open_folder_form(self, pane_id):
        """Popup that moves a reverse pane to another client folder."""
        root = self._reverse_root(self._runtime(pane_id))
        if not root:
            self._flash("리버스 에이전트 pane에서만 폴더를 바꿀 수 있어요")
            return
        text = self._tilde(root).rstrip("/") + "/"
        self.form = {"kind": "folder", "pane": pane_id, "current": root,
                     "path": text, "caret": {"path": len(text)},
                     "view": {}, "sel": -1, "scroll": 0, "error": None,
                     "opened": time.time(), "cursor": None, "hits": []}
        self.explorer_open = False
        self.reverse_open = False
        self.help_open = False
        if self.sidebar_focus:
            self._leave_sidebar()
        self._focus_dirty = True

    def _folder_target(self, text):
        """Absolute path for the typed text (relative = from the current
        folder), without resolving symlinks the user typed."""
        return os.path.normpath(os.path.join(
            self.form["current"], os.path.expanduser(text.strip())))

    def _folder_matches(self):
        """Sub-folders of the typed path's parent starting with its last
        component (case-insensitive; dot-folders only when asked for)."""
        form = self.form
        text = form["path"]
        head, prefix = text[:text.rfind("/") + 1], text[text.rfind("/") + 1:]
        key = (head, prefix)
        if form.get("match_key") == key:
            return form["matches"]
        base = self._folder_target(head) if head else form["current"]
        names = []
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if prefix and not entry.name.lower().startswith(
                            prefix.lower()):
                        continue
                    if entry.name.startswith(".") and \
                            not prefix.startswith("."):
                        continue
                    try:
                        if entry.is_dir():
                            names.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            pass
        names.sort(key=lambda n: (n.lower(), n))
        form["match_key"], form["matches"] = key, names[:500]
        return form["matches"]

    def _folder_set(self, text):
        form = self.form
        form["path"] = text
        form["caret"]["path"] = len(text)
        form["sel"], form["scroll"], form["error"] = -1, 0, None

    def _folder_pick(self, index):
        """Descend into a listed folder (fills the path, keeps the popup)."""
        text = self.form["path"]
        names = self._folder_matches()
        if 0 <= index < len(names):
            self._folder_set(text[:text.rfind("/") + 1] + names[index] + "/")

    def _folder_complete(self):
        """Tab: one match → fill it; several → extend to their common
        prefix, or start choosing from the list."""
        form = self.form
        names = self._folder_matches()
        if not names:
            return
        if len(names) == 1:
            self._folder_pick(0)
            return
        text = form["path"]
        typed = text[text.rfind("/") + 1:]
        common = os.path.commonprefix([n.lower() for n in names])
        if len(common) > len(typed):
            self._folder_set(text[:text.rfind("/") + 1]
                             + names[0][:len(common)])
        else:
            form["sel"] = (form["sel"] + 1) % len(names)

    def _folder_submit(self):
        form = self.form
        target = self._folder_target(form["path"])
        if not form["path"].strip() or not os.path.isdir(target):
            form["error"] = "폴더가 아니에요: %s" % self._tilde(target)
            return None
        if os.path.realpath(target) == os.path.realpath(form["current"]):
            form["error"] = "지금 작업 폴더와 같아요"
            return None
        pane_id = form["pane"]
        self._close_host_form()
        return AttachResult("reverse-folder", (pane_id, target))

    def _folder_key(self, unit):
        form = self.form
        mouse = MOUSE_RE.match(unit)
        if mouse:
            button = int(mouse.group(1))
            if button in (64, 65):
                names = self._folder_matches()
                if names:
                    step = 1 if button == 65 else -1
                    form["sel"] = max(0, min(len(names) - 1,
                                             form["sel"] + step))
                return None
            if mouse.group(4) != b"M" or button & 96:
                return None
            gx, gy = int(mouse.group(2)), int(mouse.group(3))
            for row, x0, x1, target in form["hits"]:
                if gy == row and x0 <= gx < x1:
                    if target == "save":
                        return self._folder_submit()
                    if target == "cancel":
                        self._close_host_form()
                    elif target == "path":
                        form["caret"]["path"] = self._form_click_caret(
                            form, "path", gx)
                    else:
                        self._folder_pick(target[1])
                    return None
            y0, x0, y1, x1 = form.get("box") or (0, 0, 10 ** 6, 10 ** 6)
            if not (y0 <= gy <= y1 and x0 <= gx <= x1):
                self._close_host_form()  # click outside = cancel
            return None
        if unit == b"\x1b":
            self._close_host_form()
            return None
        if unit in (b"\r", b"\n"):
            if form["sel"] >= 0:
                self._folder_pick(form["sel"])
                return None
            return self._folder_submit()
        if unit == b"\t":
            self._folder_complete()
            return None
        if unit in (b"\x1b[A", b"\x1b[B", b"\x1b[Z"):
            names = self._folder_matches()
            if names:
                step = 1 if unit == b"\x1b[B" else -1
                form["sel"] = max(-1, min(len(names) - 1, form["sel"] + step))
            return None
        if self._text_edit(form, "path", unit, path=True):
            form["sel"], form["scroll"], form["error"] = -1, 0, None
        return None

    def _text_edit(self, form, field, unit, path=False):
        """Line editing for a popup text field (caret moves, Home/End,
        ^A ^E ^U ^W, Backspace/Delete, typing). True if the text changed."""
        value = form[field]
        caret = form["caret"]
        at = min(caret[field], len(value))
        if unit in (b"\x1b[C", b"\x1b[D"):
            step = 1 if unit == b"\x1b[C" else -1
            caret[field] = max(0, min(len(value), at + step))
            return False
        if unit in (b"\x1b[H", b"\x1bOH", b"\x1b[1~", b"\x01"):
            caret[field] = 0
            return False
        if unit in (b"\x1b[F", b"\x1bOF", b"\x1b[4~", b"\x05"):
            caret[field] = len(value)
            return False
        if unit in (b"\x7f", b"\x08"):
            if not at:
                return False
            value, at = value[:at - 1] + value[at:], at - 1
        elif unit in (b"\x1b[3~", b"\x04"):
            value = value[:at] + value[at + 1:]
        elif unit == b"\x15":  # ^U: clear before the caret
            value, at = value[at:], 0
        elif unit == b"\x17":  # ^W: previous word / path component
            cut = value[:at].rstrip("/" if path else " ")
            cut = cut[:cut.rfind("/" if path else " ") + 1]
            value, at = cut + value[at:], len(cut)
        elif unit.startswith(b"\x1b"):
            return False  # bracketed-paste markers and other sequences
        else:
            text = unit.decode(errors="ignore").replace("\n", "")
            if not (text and text.isprintable()):
                return False
            value, at = value[:at] + text + value[at:], at + len(text)
        form[field], caret[field] = value, at
        return True

    def _text_field(self, form, field, row, col, width, label, label_w,
                    body, inner):
        """One labelled input box line; col is the 0-based column of the
        box. Records the click target and the text cursor."""
        form["hits"].append((row, col + 1, col + 1 + width, field))
        value = form[field]
        caret = min(form["caret"][field], len(value))
        start = min(form["view"].get(field, (0, 0))[1], caret)
        while self._width(value[start:caret]) > width - 2:
            start += 1
        form["view"][field] = (col + 1, start)
        form["cursor"] = (row, col + 1 + self._width(value[start:caret]))
        return (body + "  " + "\x1b[1;48;5;235;38;5;51m"
                + self._pad(label, label_w) + "\x1b[0;48;5;238;38;5;255m "
                + self._pad(value[start:], width - 1) + body
                + " " * max(0, inner - 2 - label_w - width))

    def _folder_lines(self, form, top, left, inner, body, dim):
        """Rows of the folder popup: current folder, the path field, the
        matching sub-folders and a note; records hits and the cursor."""
        label_w, field_w = 9, inner - 13
        lines = []

        def plain(text, style=body):
            lines.append(style + self._pad(text, inner))

        def row():
            return top + len(lines) + 2  # 1-based screen row of next line

        plain("")
        plain("  %s%s" % (self._pad("지금", label_w),
                          self._tilde(form["current"])), dim)
        lines.append(self._text_field(form, "path", row(),
                                      left + 4 + label_w, field_w, "새 폴더",
                                      label_w, body, inner))
        plain("")
        names = self._folder_matches()
        visible = self.FOLDER_ROWS
        sel = form["sel"]
        if sel >= 0:
            if sel < form["scroll"]:
                form["scroll"] = sel
            elif sel >= form["scroll"] + visible:
                form["scroll"] = sel - visible + 1
        form["scroll"] = max(0, min(form["scroll"],
                                    max(0, len(names) - visible)))
        for i in range(visible):
            index = form["scroll"] + i
            if index >= len(names):
                if i == 0:
                    plain("    (하위 폴더 없음)", dim)
                else:
                    plain("")
                continue
            more = (i == visible - 1 and index < len(names) - 1)
            label = "… 외 %d개" % (len(names) - index) if more \
                else names[index] + "/"
            on = index == sel and not more
            style = "\x1b[0;1;48;5;24;38;5;231m" if on else body
            if not more:
                form["hits"].append((row(), left + 4,
                                     left + 2 + inner - 2, ("pick", index)))
            lines.append(body + "  " + style
                         + self._pad(("▸ " if on else "  ") + label,
                                     inner - 4) + body + "  ")
        plain("")
        if form["error"]:
            plain("  " + form["error"], "\x1b[0;48;5;235;38;5;203m")
        else:
            plain("  이 폴더에서 에이전트를 새 대화로 다시 시작해요", dim)
        plain("")
        lines.append(self._popup_buttons(form, row(), left, inner, body,
                                         (("save", "  변경  "),
                                          ("cancel", "  취소  "))))
        return lines

    def _explorer_chunk(self):
        _x, _y, _w, area_h = self._area()
        width = self._sidebar_span()[0]
        height = area_h
        inner = width - 2
        out = []
        col = 1
        refresh = " ↻ "
        room = inner - self._width(refresh)
        title = " Files: " + (self.explorer_path or "")
        if self._width(title) > room:  # keep the path's tail, by width
            kept, used = "", 0
            for char in reversed(self.explorer_path or ""):
                w = char_width(char)
                if used + w > room - 9:
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
                   + ("─" * max(0, room - self._width(title))).encode()
                   + bg + b"\x1b[38;5;31m" + refresh.encode() + edge
                   + "┐".encode() + b"\x1b[0m")
        self._explorer_refresh_hit = (col + 1 + room, col + 1 + inner)
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
        if key in ("{", "}"):
            self._swap_step(-1 if key == "{" else 1)
            return None
        if key == "!":
            if len(self.runtimes) > 1:  # tmux break-pane
                return AttachResult("move-pane", (self.active_id, None))
            self._flash("pane이 하나뿐이라 떼어낼 수 없어요")
            return None
        if key == "e":
            self._toggle_explorer()
            return None
        if key == "a":
            self._toggle_reverse()
            return None
        if key == "f":
            self._open_folder_form(self.active_id)
            return None
        if key == "h":
            if self.sidebar_focus:
                self._leave_sidebar()
            else:
                self._focus_sidebar()
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
            return self._sessions_menu()
        if key == ",":
            self._rename_window_prompt()
            return None
        if key == "$":
            self._rename_session_prompt(self._session_name)
            return None
        if key == "x":
            rect = rects.get(self.active_id)
            self._menu_at = (rect[0] + rect[2] // 2 - 8,
                             rect[1] + rect[3] // 2) if rect else (1, 1)
            self._confirm_kill_pane(self.active_id)
            return None
        if key == "?":
            self.help_open = True
            self._help_opened_at = time.time()
            self._help_full = False
            self._focus_dirty = True
            return None
        return None  # unknown prefix keys are swallowed, like tmux
