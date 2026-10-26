"""Flight recorder for the outer terminal.

Keeps the last bytes crow wrote to the real terminal and a timeline of
input events in memory. When the terminal app dies the client gets
SIGHUP; the recorder then dumps everything to ~/.crow-cli/crash/ so the
exact output that preceded a terminal crash can be replayed later.

Typed text is never stored, only its length (passwords go through
panes); escape sequences such as mouse reports are kept verbatim.
"""
import collections
import os
import re
import signal
import threading
import time

from . import registry as registry_mod

OUT_LIMIT = 2 * 1024 * 1024
EVENT_LIMIT = 4000
KEEP = 5
_SEQ = re.compile(rb"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|O.|\][^\x07]*\x07)")

_lock = threading.Lock()
_out = collections.deque()
_out_size = 0
_events = collections.deque(maxlen=EVENT_LIMIT)
_installed = False


def note(kind, detail=""):
    with _lock:
        _events.append((time.time(), kind, detail))


def wrote(data):
    global _out_size
    if not data:
        return
    with _lock:
        _out.append((time.time(), data))
        _out_size += len(data)
        while _out_size > OUT_LIMIT and len(_out) > 1:
            _out_size -= len(_out.popleft()[1])
        _events.append((time.time(), "out", "%d bytes" % len(data)))


def typed(data):
    seqs = _SEQ.findall(data)
    plain = len(_SEQ.sub(b"", data))
    detail = " ".join(repr(s)[2:-1] for s in seqs[:8])
    if plain:
        detail += (" " if detail else "") + "<%d key bytes>" % plain
    note("in", detail)


class Writer(object):
    """Wraps the binary stdout so every write is recorded."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, data):
        wrote(bytes(data))
        return self.raw.write(data)

    def flush(self):
        return self.raw.flush()


def dump(reason):
    folder = os.path.join(registry_mod.home(), "crash")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    # Closing a window also sends SIGHUP: keep only the latest few.
    old = sorted(f for f in os.listdir(folder) if f.endswith(".out"))
    for name in old[:-(KEEP - 1)] if len(old) >= KEEP else []:
        for ext in (".out", ".log"):
            try:
                os.unlink(os.path.join(folder, name[:-4] + ext))
            except OSError:
                pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder, "%s-%d" % (stamp, os.getpid()))
    with _lock:
        out = list(_out)
        events = list(_events)
    fd = os.open(base + ".out", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        for _, data in out:
            fh.write(data)
    fd = os.open(base + ".log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("reason: %s\nterm: %s %s\n" % (
            reason, os.environ.get("TERM_PROGRAM", "?"),
            os.environ.get("TERM_PROGRAM_VERSION", "")))
        for ts, kind, detail in events:
            fh.write("%s.%03d %-6s %s\n" % (
                time.strftime("%H:%M:%S", time.localtime(ts)),
                int(ts * 1000) % 1000, kind, detail))
    return base


def install():
    """Dump on SIGHUP (terminal window/app gone), then exit as before."""
    global _installed
    if _installed:
        return
    _installed = True

    def on_hup(signo, _frame):
        try:
            dump("SIGHUP")
        finally:
            signal.signal(signo, signal.SIG_DFL)
            os.kill(os.getpid(), signo)
    signal.signal(signal.SIGHUP, on_hup)
