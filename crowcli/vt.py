"""Minimal VT100/xterm screen emulator for one pane.

Not a full xterm: it covers what shells and the common agent TUIs
(claude/codex/grok, vim, top, less) emit — cursor motion, erase/insert/
delete, scroll regions, alternate screen, SGR (incl. 256-colour and
truecolour), wide/combining characters, mouse/bracketed-paste mode
tracking, and DSR/DA replies. Each pane owns one Screen; the attach
renderer composites the grids.
"""
import codecs
import unicodedata

DEFAULT_ATTR = (None, None, False, False, False, False, False, False)
# attr = (fg, bg, bold, dim, italic, underline, reverse, strike)
# fg/bg = None | ("p", palette_index) | ("rgb", r, g, b)


def _blank(attr=DEFAULT_ATTR):
    return [" ", attr]


def char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class Screen(object):
    def __init__(self, cols=80, rows=24):
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "ground"
        self.csi = ""
        self.osc = False
        self.esc_intermediate = ""
        self.grid = [self._line() for _ in range(self.rows)]
        self.scrollback = []
        self.cx = 0
        self.cy = 0
        self.attr = DEFAULT_ATTR
        self.saved = (0, 0, DEFAULT_ATTR)
        self.alt_saved = (0, 0, DEFAULT_ATTR)
        self.main_grid = None
        self.main_scrollback = None
        self.alt = False
        self.top = 0
        self.bottom = self.rows - 1
        self.origin = False
        self.insert = False
        self.wrap_pending = False
        self.cursor_visible = True
        self.app_cursor = False
        self.mouse_mode = 0
        self.mouse_sgr = False
        self.bracketed_paste = False
        self.last_char = None
        self._eol_candidate = None  # possible zsh prompt_sp remnant line
        self.responses = []  # bytes the client must send back to the PTY
        self.dirty = set(range(self.rows))

    # -- basics ------------------------------------------------------------
    def _line(self, attr=DEFAULT_ATTR):
        return [_blank(attr) for _ in range(self.cols)]

    def resize(self, cols, rows):
        cols, rows = max(1, cols), max(1, rows)
        grid = []
        for y in range(rows):
            line = self.grid[y][:cols] if y < len(self.grid) else []
            line = [list(cell) for cell in line]
            line += [_blank() for _ in range(cols - len(line))]
            grid.append(line)
        self.cols, self.rows, self.grid = cols, rows, grid
        self.top, self.bottom = 0, rows - 1
        self.cx = min(self.cx, cols - 1)
        self.cy = min(self.cy, rows - 1)
        self.wrap_pending = False
        self.dirty = set(range(rows))

    def lines_for_view(self, offset):
        """Grid lines to show when scrolled back `offset` lines."""
        if offset <= 0:
            return self.grid
        offset = min(offset, len(self.scrollback))
        combined = self.scrollback + self.grid
        end = len(combined) - offset
        return combined[max(0, end - self.rows):end]

    def feed(self, data):
        for char in self.decoder.decode(data):
            self._char(char)

    def mark_clean(self):
        self.dirty = set()

    # -- parser --------------------------------------------------------------
    def _char(self, char):
        if self.state == "esc":
            self._eol_candidate = None
            self._escape(char)
            return
        if self.state == "csi":
            if "@" <= char <= "~":
                self._csi(self.csi, char)
                self.state = "ground"
                self.csi = ""
            else:
                self.csi += char
            return
        if self.state == "osc":
            if char == "\x07":
                self.state = "ground"
            elif char == "\x1b":
                self.state = "esc_string"
            return
        if self.state == "esc_string":  # string terminator after ESC
            self.state = "ground" if char == "\\" else "osc"
            return
        if self.state == "string":  # DCS/SOS/PM/APC body
            if char == "\x1b":
                self.state = "esc_string"
            return
        # ground
        if char == "\x1b":
            self.state = "esc"
            self.esc_intermediate = ""
        elif char == "\x9b":
            self.state = "csi"
            self.csi = ""
        elif char in ("\x90", "\x98", "\x9e", "\x9f"):
            self.state = "string"
        elif char == "\x9d":
            self.state = "osc"
        elif char == "\r":
            self.cx = 0
            self.wrap_pending = False
        elif char in ("\n", "\x0b", "\x0c"):
            self._eol_candidate = None
            self._linefeed()
        elif char == "\x08":
            self._eol_candidate = None
            self.cx = max(0, self.cx - 1)
            self.wrap_pending = False
        elif char == "\t":
            self._eol_candidate = None
            self.cx = min(self.cols - 1, (self.cx // 8 + 1) * 8)
            self.wrap_pending = False
        elif char < " " or char == "\x7f":
            pass
        else:
            self._print(char)

    def _escape(self, char):
        if self.esc_intermediate:
            self.esc_intermediate = ""
            self.state = "ground"
            return
        if char in "()#%*+":
            self.esc_intermediate = char
            return
        self.state = "ground"
        if char == "[":
            self.state = "csi"
            self.csi = ""
        elif char == "]":
            self.state = "osc"
        elif char in "PX^_":
            self.state = "string"
        elif char == "c":
            self._reset()
        elif char == "7":
            self.saved = (self.cx, self.cy, self.attr)
        elif char == "8":
            self.cx, self.cy, self.attr = self.saved
            self._clamp_cursor()
        elif char == "D":
            self._linefeed()
        elif char == "E":
            self.cx = 0
            self._linefeed()
        elif char == "M":
            self._reverse_index()
        elif char == "=":
            self.app_cursor = True
        elif char == ">":
            self.app_cursor = False
        elif char == "\\":
            pass

    def _reset(self):
        self.grid = [self._line() for _ in range(self.rows)]
        self.scrollback = []
        self.cx = self.cy = 0
        self.attr = DEFAULT_ATTR
        self.top, self.bottom = 0, self.rows - 1
        self.origin = self.insert = self.wrap_pending = self.alt = False
        self.main_grid = None
        self.mouse_mode = 0
        self.mouse_sgr = self.bracketed_paste = False
        self.cursor_visible = True
        self.dirty = set(range(self.rows))

    # -- screen operations -------------------------------------------------
    def _clamp_cursor(self):
        self.cx = max(0, min(self.cx, self.cols - 1))
        low = self.top if self.origin else 0
        high = self.bottom if self.origin else self.rows - 1
        self.cy = max(low, min(self.cy, high))

    def _linefeed(self):
        self.wrap_pending = False
        if self.cy == self.bottom:
            self._scroll_up(self.top, self.bottom, 1)
        elif self.cy < self.rows - 1:
            self.cy += 1

    def _reverse_index(self):
        self.wrap_pending = False
        if self.cy == self.top:
            self._scroll_down(self.top, self.bottom, 1)
        elif self.cy > 0:
            self.cy -= 1

    def _scroll_up(self, top, bottom, count):
        for _ in range(min(count, bottom - top + 1)):
            line = self.grid.pop(top)
            if not self.alt and top == 0:
                self.scrollback.append(line)
                del self.scrollback[:-10000]
            self.grid.insert(bottom, self._line())
        for y in range(top, bottom + 1):
            self.dirty.add(y)

    def _scroll_down(self, top, bottom, count):
        for _ in range(min(count, bottom - top + 1)):
            self.grid.pop(bottom)
            self.grid.insert(top, self._line())
        for y in range(top, bottom + 1):
            self.dirty.add(y)

    def _print(self, char):
        width = char_width(char)
        if width == 0:
            if self.last_char is not None:
                y, x = self.last_char
                if 0 <= y < self.rows and 0 <= x < self.cols:
                    self.grid[y][x][0] += char
                    self.dirty.add(y)
            return
        if self.wrap_pending:
            if char == " ":
                # zsh's prompt_sp fills the line with spaces to force a
                # wrap, then erases its '%' mark on the *same* line with
                # CR+space. When the stream was recorded at a different
                # width (replay after resize/reattach), the fill wraps
                # here and the mark would survive as a stray line.
                # Remember such a line; if real output follows on the
                # next line, blank the mark (see below).
                line = self.grid[self.cy]
                filled = [(x, c) for x, c in enumerate(line)
                          if c[0] not in (" ", "")]
                if len(filled) == 1 and filled[0][0] == 0 \
                        and filled[0][1][0] == "%":
                    self._eol_candidate = line
            self.cx = 0
            self._linefeed()
        self.dirty.add(self.cy)
        if self.insert:
            line = self.grid[self.cy]
            for _ in range(width):
                line.insert(self.cx, _blank(self.attr))
                line.pop()
        if char != " " and self._eol_candidate is not None:
            candidate, self._eol_candidate = self._eol_candidate, None
            if candidate[0][0] == "%":
                candidate[0] = _blank()
                for y, line in enumerate(self.grid):
                    if line is candidate:
                        self.dirty.add(y)
                        break
        self.grid[self.cy][self.cx] = [char, self.attr]
        self.last_char = (self.cy, self.cx)
        if width == 2 and self.cx + 1 < self.cols:
            self.grid[self.cy][self.cx + 1] = ["", self.attr]
        if self.cx + width >= self.cols:
            self.cx = self.cols - 1
            self.wrap_pending = True
        else:
            self.cx += width

    # -- CSI -------------------------------------------------------------------
    def _params(self, raw, default=1):
        parts = raw.lstrip("?<>=!").split(";")
        values = []
        for part in parts:
            try:
                values.append(int(part) if part else 0)
            except ValueError:
                values.append(0)
        return values or [default]

    def _csi(self, raw, final):
        self._eol_candidate = None
        private = raw.startswith("?")
        values = self._params(raw)
        n = values[0] or 1
        if final == "m" and not private:
            self._sgr(raw.lstrip("?<>=!"))
        elif final == "A":
            self.cy = max(self.top if self.origin else 0, self.cy - n)
            self.wrap_pending = False
        elif final in ("B", "e"):
            self.cy = min(self.bottom if self.origin else self.rows - 1,
                          self.cy + n)
            self.wrap_pending = False
        elif final in ("C", "a"):
            self.cx = min(self.cols - 1, self.cx + n)
            self.wrap_pending = False
        elif final == "D":
            self.cx = max(0, self.cx - n)
            self.wrap_pending = False
        elif final == "E":
            self.cy = min(self.rows - 1, self.cy + n)
            self.cx = 0
        elif final == "F":
            self.cy = max(0, self.cy - n)
            self.cx = 0
        elif final in ("G", "`"):
            self.cx = min(self.cols - 1, n - 1)
        elif final == "d":
            self.cy = min(self.rows - 1, n - 1)
            if self.origin:
                self.cy += self.top
        elif final in ("H", "f"):
            row = (values[0] if len(values) > 0 and values[0] else 1) - 1
            col = (values[1] if len(values) > 1 and values[1] else 1) - 1
            self.cy = row + (self.top if self.origin else 0)
            self.cx = col
            self._clamp_cursor()
            self.wrap_pending = False
        elif final == "I":
            self.cx = min(self.cols - 1, (self.cx // 8 + 1) * 8 * n)
        elif final == "Z":
            self.cx = max(0, ((self.cx - 1) // 8) * 8 if self.cx else 0)
        elif final == "J":
            self._erase_display(values[0])
        elif final == "K":
            self._erase_line(values[0])
        elif final == "X":
            line = self.grid[self.cy]
            for x in range(self.cx, min(self.cols, self.cx + n)):
                line[x] = _blank(self.attr)
            self.dirty.add(self.cy)
        elif final == "P":
            line = self.grid[self.cy]
            del line[self.cx:self.cx + n]
            line += [_blank(self.attr)] * min(n, self.cols - len(line))
            self.dirty.add(self.cy)
        elif final == "@":
            line = self.grid[self.cy]
            for _ in range(n):
                line.insert(self.cx, _blank(self.attr))
                line.pop()
            self.dirty.add(self.cy)
        elif final == "S":
            self._scroll_up(self.top, self.bottom, n)
        elif final == "T":
            if len(values) <= 1:
                self._scroll_down(self.top, self.bottom, n)
        elif final == "L":
            if self.top <= self.cy <= self.bottom:
                self._scroll_down(self.cy, self.bottom, n)
        elif final == "M":
            if self.top <= self.cy <= self.bottom:
                self._scroll_up(self.cy, self.bottom, n)
        elif final == "r" and not private:
            self.top = min(self.rows - 1, (values[0] or 1) - 1)
            second = values[1] if len(values) > 1 else 0
            self.bottom = min(self.rows - 1, (second or self.rows) - 1)
            if self.bottom < self.top:
                self.top, self.bottom = 0, self.rows - 1
            self.cx = self.cy = 0
            if self.origin:
                self.cy = self.top
        elif final == "s":
            self.saved = (self.cx, self.cy, self.attr)
        elif final == "u" and not private:
            self.cx, self.cy, self.attr = self.saved
            self._clamp_cursor()
        elif final == "b":
            if self.last_char is not None:
                y, x = self.last_char
                char = self.grid[y][x][0]
                for _ in range(n):
                    self._print(char)
        elif final in ("h", "l"):
            self._set_modes(values, final == "h", private)
        elif final == "n" and not private:
            if values[0] == 6:
                self.responses.append(
                    ("\x1b[%d;%dR" % (self.cy + 1, self.cx + 1)).encode())
        elif final == "c" and not private:
            self.responses.append(b"\x1b[?62;22c")
        elif final == "x":
            self.responses.append(b"\x1b[3;1;1;120;120;1;0x")

    def _erase_display(self, mode):
        if mode == 0:
            self._erase_line(0)
            for y in range(self.cy + 1, self.rows):
                self.grid[y] = self._line()
                self.dirty.add(y)
        elif mode == 1:
            self._erase_line(1)
            for y in range(0, self.cy):
                self.grid[y] = self._line()
                self.dirty.add(y)
        elif mode in (2, 3):
            self.grid = [self._line() for _ in range(self.rows)]
            if mode == 3:
                self.scrollback = []
            self.dirty = set(range(self.rows))

    def _erase_line(self, mode):
        line = self.grid[self.cy]
        if mode == 0:
            span = range(self.cx, self.cols)
        elif mode == 1:
            span = range(0, self.cx + 1)
        else:
            span = range(0, self.cols)
        for x in span:
            line[x] = _blank(self.attr)
        self.dirty.add(self.cy)

    def _set_modes(self, values, on, private):
        for value in values:
            if not private:
                if value == 4:
                    self.insert = on
                continue
            if value == 25:
                self.cursor_visible = on
            elif value == 6:
                self.origin = on
            elif value == 1:
                self.app_cursor = on
            elif value in (47, 1047):
                self._alt_screen(on, save=False)
            elif value == 1048:
                if on:
                    self.alt_saved = (self.cx, self.cy, self.attr)
                else:
                    self.cx, self.cy, self.attr = self.alt_saved
                    self._clamp_cursor()
            elif value == 1049:
                self._alt_screen(on, save=True)
            elif value in (1000, 1002, 1003):
                self.mouse_mode = value if on else 0
            elif value == 1006:
                self.mouse_sgr = on
            elif value == 2004:
                self.bracketed_paste = on

    def _alt_screen(self, on, save):
        if on == self.alt:
            return
        if on:
            if save:
                self.alt_saved = (self.cx, self.cy, self.attr)
            self.main_grid = self.grid
            self.main_scrollback = self.scrollback
            self.grid = [self._line() for _ in range(self.rows)]
            self.scrollback = []
            self.alt = True
        else:
            self.grid = self.main_grid or [self._line()
                                           for _ in range(self.rows)]
            self.scrollback = self.main_scrollback or []
            self.main_grid = None
            self.alt = False
            if save:
                self.cx, self.cy, self.attr = self.alt_saved
                self._clamp_cursor()
        self.dirty = set(range(self.rows))

    def _sgr(self, raw):
        parts = raw.split(";") if raw else ["0"]
        values = []
        for part in parts:
            try:
                values.append(int(part) if part else 0)
            except ValueError:
                values.append(0)
        fg, bg, bold, dim, italic, underline, reverse, strike = self.attr
        i = 0
        while i < len(values):
            value = values[i]
            if value == 0:
                fg, bg, bold, dim, italic, underline, reverse, strike = \
                    DEFAULT_ATTR
            elif value == 1:
                bold = True
            elif value == 2:
                dim = True
            elif value == 3:
                italic = True
            elif value == 4:
                underline = True
            elif value == 7:
                reverse = True
            elif value == 9:
                strike = True
            elif value == 22:
                bold = dim = False
            elif value == 23:
                italic = False
            elif value == 24:
                underline = False
            elif value == 27:
                reverse = False
            elif value == 29:
                strike = False
            elif 30 <= value <= 37:
                fg = ("p", value - 30)
            elif value == 39:
                fg = None
            elif 40 <= value <= 47:
                bg = ("p", value - 40)
            elif value == 49:
                bg = None
            elif 90 <= value <= 97:
                fg = ("p", value - 90 + 8)
            elif 100 <= value <= 107:
                bg = ("p", value - 100 + 8)
            elif value in (38, 48):
                colour, used = None, 0
                if i + 2 < len(values) and values[i + 1] == 5:
                    colour, used = ("p", values[i + 2]), 2
                elif i + 4 < len(values) and values[i + 1] == 2:
                    colour = ("rgb", values[i + 2], values[i + 3],
                              values[i + 4])
                    used = 4
                if value == 38:
                    fg = colour if colour else fg
                else:
                    bg = colour if colour else bg
                i += used
            i += 1
        self.attr = (fg, bg, bold, dim, italic, underline, reverse, strike)


def sgr_for(attr, current):
    """ANSI bytes switching from current attr to attr (renderer helper)."""
    if attr == current:
        return b""
    fg, bg, bold, dim, italic, underline, reverse, strike = attr
    codes = ["0"]
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if italic:
        codes.append("3")
    if underline:
        codes.append("4")
    if reverse:
        codes.append("7")
    if strike:
        codes.append("9")

    def colour(value, base, bright_base, prefix):
        if value is None:
            return None
        if value[0] == "p":
            idx = value[1]
            if idx < 8:
                return str(base + idx)
            if idx < 16:
                return str(bright_base + idx - 8)
            return "%d;5;%d" % (prefix, idx)
        return "%d;2;%d;%d;%d" % (prefix, value[1], value[2], value[3])

    fg_code = colour(fg, 30, 90, 38)
    bg_code = colour(bg, 40, 100, 48)
    if fg_code:
        codes.append(fg_code)
    if bg_code:
        codes.append(bg_code)
    return ("\x1b[" + ";".join(codes) + "m").encode()
