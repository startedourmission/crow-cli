"""Clipboard image paste (Crow's ClipboardImage): read a PNG from the Mac
clipboard, store it where the pane's program can open it (a private temp
dir on the server for remote panes), and paste the path as plain text."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

SIZE_LIMIT = 20 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SAFE_PATH = re.compile(r"^[A-Za-z0-9/._-]+$")

# Runs on the server: a fresh 0700 dir, the PNG from stdin, its path back.
REMOTE_SAVE = ('umask 077; t=${TMPDIR:-/tmp}; d=$(mktemp -d "${t%/}/'
               'crow-clipboard-XXXXXXXX") && cat > "$d/image.png" && '
               'printf "%s\\n" "$d/image.png"')


def validate(data):
    if len(data) > SIZE_LIMIT:
        raise ValueError("클립보드 이미지가 20MB를 넘어요")
    if not data.startswith(PNG_MAGIC):
        raise ValueError("클립보드 이미지가 PNG가 아니에요")


def _osascript_dump(klass, target):
    script = [
        "try",
        "set d to (the clipboard as «class %s»)" % klass,
        "on error",
        'return "none"',
        "end try",
        'set f to open for access (POSIX file "%s") with write permission'
        % target,
        "set eof f to 0",
        "write d to f",
        "close access f",
        'return "ok"',
    ]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    done = subprocess.run(args, capture_output=True, text=True, timeout=10,
                          stdin=subprocess.DEVNULL)
    return done.returncode == 0 and done.stdout.strip() == "ok"


def read_png():
    """PNG bytes of the clipboard image, or None if it holds no image."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    work = tempfile.mkdtemp(prefix="crow-clip-read-")
    try:
        png = os.path.join(work, "image.png")
        if not _osascript_dump("PNGf", png):
            tiff = os.path.join(work, "image.tiff")
            if not _osascript_dump("TIFF", tiff):
                return None
            subprocess.run(["sips", "-s", "format", "png", tiff, "--out", png],
                           capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
        if not os.path.exists(png) or os.path.getsize(png) > SIZE_LIMIT:
            if os.path.exists(png):
                raise ValueError("클립보드 이미지가 20MB를 넘어요")
            return None
        with open(png, "rb") as stream:
            data = stream.read()
        validate(data)
        return data
    finally:
        shutil.rmtree(work, ignore_errors=True)


def save_locally(data):
    validate(data)
    directory = tempfile.mkdtemp(prefix="crow-clipboard-")
    os.chmod(directory, 0o700)
    path = os.path.join(directory, "image.png")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return path


def upload(ssh_args, data):
    """Save on the server over ssh; returns the server path."""
    validate(data)
    done = subprocess.run(list(ssh_args) + [REMOTE_SAVE], input=data,
                          capture_output=True, timeout=60)
    lines = done.stdout.decode(errors="replace").strip().splitlines()
    if done.returncode != 0 or not lines:
        detail = done.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else
                           "ssh 종료 %d" % done.returncode)
    return lines[-1]


def pasted_path(path):
    """No newline, no command: just the (shell-quoted) path and a space."""
    if SAFE_PATH.match(path):
        return path + " "
    return "'" + path.replace("'", "'\\''") + "' "
