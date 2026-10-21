"""Client side of the crow-session daemon protocol.

The daemon itself is src/crow-session.c (recovered from Crow). This module
locates/builds that binary and speaks its frame protocol over the private
Unix socket: 1-byte kind + uint32 big-endian length.

  c->d  A attach | D data | W winsize(col,row u16be) | K kill | I info
  d->c  O output | I cwd    | X exit(u32be code)
"""
import os
import socket
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def runtime_root():
    override = os.environ.get("CROWCLI_SESSION_ROOT") or \
        os.environ.get("CROW_SESSION_ROOT")
    if override:
        return override
    return "/tmp/crow-cli-sessions-%d" % os.getuid()


def socket_path(session_id, root=None):
    return os.path.join(root or runtime_root(), session_id + ".sock")


def exit_path(session_id, root=None):
    return os.path.join(root or runtime_root(), session_id + ".exit")


def binary():
    """Locate crow-session, building it with cc when needed (Crow did the
    same: system toolchain, no Homebrew/tmux required)."""
    candidates = []
    if os.environ.get("CROWCLI_SESSION_BIN"):
        candidates.append(os.environ["CROWCLI_SESSION_BIN"])
    candidates.append(os.path.join(REPO, "build", "crow-session"))
    from .registry import home
    candidates.append(os.path.join(home(), "bin", "crow-session"))
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return build_binary(candidates[1] if not os.environ.get(
        "CROWCLI_SESSION_BIN") else candidates[0])


def build_binary(dest=None):
    from .registry import home
    dest = dest or os.path.join(home(), "bin", "crow-session")
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    src = os.path.join(REPO, "src", "crow-session.c")
    if not os.path.exists(src):  # installed layout: source shipped alongside
        src = os.path.join(os.path.dirname(dest), "crow-session.c")
    args = ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            src, "-o", dest + ".tmp"]
    if sys.platform.startswith("linux"):
        args.append("-lutil")
    subprocess.run(args, check=True)
    os.chmod(dest + ".tmp", 0o700)
    os.replace(dest + ".tmp", dest)
    return dest


def frame(kind, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode()
    return kind.encode() + struct.pack(">I", len(payload)) + payload


class Daemon(object):
    """One connection to a window's daemon."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    @classmethod
    def connect(cls, session_id, timeout=5.0, root=None):
        path = socket_path(session_id, root)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError, socket.error):
            sock.close()
            return None
        return cls(sock)

    def send(self, kind, payload=b""):
        self.sock.sendall(frame(kind, payload))

    def send_size(self, cols, rows):
        self.send("W", struct.pack(">HH", cols or 80, rows or 24))

    def frames(self):
        """Yield (kind, payload) for every complete frame buffered so far."""
        while len(self.buf) >= 5:
            length = struct.unpack(">I", self.buf[1:5])[0]
            if len(self.buf) < 5 + length:
                return
            kind = chr(self.buf[0])
            payload = self.buf[5:5 + length]
            self.buf = self.buf[5 + length:]
            yield kind, payload

    def feed(self, data):
        self.buf += data

    def close(self):
        try:
            self.sock.close()
        except socket.error:
            pass


def is_alive(session_id, root=None):
    conn = Daemon.connect(session_id, timeout=1.0, root=root)
    if conn is None:
        return False
    conn.close()
    return True


def has_exited(session_id, root=None):
    path = exit_path(session_id, root)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return int(fh.read().strip() or "0")
    except (ValueError, OSError):
        return 0


def spawn(session_id, cwd, command=None, root=None, env=None):
    """Start the daemon for session_id if not running. Uses the daemon's
    `serve` mode detached (start_new_session); the C attach client is
    kept for interactive/ssh attach, not for spawning."""
    if is_alive(session_id, root=root):
        return False
    if has_exited(session_id, root=root) is not None:
        raise RuntimeError(
            "session %s has ended and is not restarted; create a new window"
            % session_id)
    args = [binary(), "serve", session_id, cwd]
    if command:
        args.append(command)
    child_env = dict(os.environ)
    if root:
        child_env["CROWCLI_SESSION_ROOT"] = root
    if env:
        child_env.update(env)
    log_path = os.path.join(root or runtime_root(), session_id + ".log")
    os.makedirs(os.path.dirname(log_path), mode=0o700, exist_ok=True)
    log = open(log_path, "ab")
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=log, env=child_env, start_new_session=True,
                         close_fds=True)
    finally:
        log.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        if is_alive(session_id, root=root):
            return True
        if has_exited(session_id, root=root) is not None:
            break
        time.sleep(0.02)
    raise RuntimeError("crow-session daemon did not start for %s (see %s)"
                       % (session_id, log_path))


def stop(session_id, root=None):
    conn = Daemon.connect(session_id, timeout=2.0, root=root)
    if conn is None:
        return False
    try:
        conn.send("K")
    finally:
        conn.close()
    return True


def info(session_id, root=None):
    """Current directory of the window's child, or None."""
    conn = Daemon.connect(session_id, timeout=2.0, root=root)
    if conn is None:
        return None
    try:
        conn.send("I")
        conn.sock.settimeout(2.0)
        while True:
            data = conn.sock.recv(65536)
            if not data:
                return None
            conn.feed(data)
            for kind, payload in conn.frames():
                if kind == "I":
                    return payload.decode(errors="replace")
    except socket.error:
        return None
    finally:
        conn.close()


def capture(session_id, timeout=0.6, root=None):
    """Replay buffer of a window without sending input (attach + read)."""
    conn = Daemon.connect(session_id, timeout=2.0, root=root)
    if conn is None:
        return None
    out = []
    try:
        conn.send("A")
        conn.sock.settimeout(timeout)
        while True:
            try:
                data = conn.sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            conn.feed(data)
            for kind, payload in conn.frames():
                if kind == "O":
                    out.append(payload)
                elif kind == "X":
                    return b"".join(out)
    except socket.error:
        pass
    finally:
        conn.close()
    return b"".join(out)
