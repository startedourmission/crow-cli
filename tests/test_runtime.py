"""Exercise the real PTY daemon in an isolated runtime directory.

Ported from Crow's deleted Tools/TerminalRuntime/test_runtime.py
(commit 48cee8c), retargeted at crow-cli's binary and CROWCLI_SESSION_ROOT.
"""
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
import uuid

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "build" / "crow-session"
SOURCE = REPO / "src" / "crow-session.c"


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="ccb-", dir="/tmp")
        cls.binary = Path(cls.build.name) / "crow-session"
        args = ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                str(SOURCE), "-o", str(cls.binary)]
        if os.uname().sysname == "Linux":
            args += ["-lutil"]
        subprocess.run(args, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="ccp-", dir="/tmp")
        self.environment = dict(os.environ,
                                CROWCLI_SESSION_ROOT=self.root.name)
        self.ids = []
        self.clients = []

    def tearDown(self):
        for process, master in self.clients:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            os.close(master)
        for session in self.ids:
            subprocess.run([str(self.binary), "stop", session],
                           env=self.environment, timeout=5, check=False)
        deadline = time.monotonic() + 5
        while list(Path(self.root.name).glob("*.sock")) \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.root.cleanup()

    def attach(self, session=None, command="exec /bin/sh"):
        # Attach through the Python frame client (crowcli.client path),
        # spawning the daemon via `serve` like production code does.
        session = session or str(uuid.uuid4())
        if session not in self.ids:
            self.ids.append(session)
        env = dict(self.environment)
        log = open(os.path.join(self.root.name, session + ".log"), "ab")
        subprocess.Popen([str(self.binary), "serve", session,
                          self.root.name, command],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         env=env, start_new_session=True)
        log.close()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [str(self.binary), "attach", session, self.root.name, command],
            stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.clients.append((process, master))
        self.read_until(master, b"\x1bc")
        return session, process, master

    def read_until(self, fd, marker):
        data = b""
        deadline = time.monotonic() + 5
        while marker not in data and time.monotonic() < deadline:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    data += os.read(fd, 65536)
                except OSError:
                    break
        self.assertIn(marker, data)
        return data

    def test_detach_reattach_keeps_process_and_does_not_relaunch(self):
        session, client, master = self.attach()
        os.write(master, b"printf '__PID_%s__\\n' $$\n")
        data = self.read_until(master, b"__\r\n")
        pid = re.search(rb"__PID_(\d+)__", data).group(1)
        os.write(master, b"sleep 0.3; printf '__BG_%s__\\n' DONE; "
                         b"printf saved > persisted\n")
        client.terminate()
        client.wait(timeout=5)
        deadline = time.monotonic() + 5
        saved = Path(self.root.name) / "persisted"
        while not saved.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(saved.read_text(), "saved")
        _, _, attached = self.attach(session, command="exit 99")
        os.write(attached, b"printf '__SAME_%s__\\n' $$\n")
        self.assertIn(b"__SAME_" + pid + b"__",
                      self.read_until(attached, b"__SAME_" + pid + b"__"))

    def test_resize_and_current_directory(self):
        session, _, master = self.attach()
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", 40, 100, 0, 0))
        os.kill(self.clients[-1][0].pid, signal.SIGWINCH)
        os.write(master, b"printf '__SIZE__'; stty size\n")
        self.read_until(master, b"__SIZE__40 100")
        child = Path(self.root.name) / "child"
        child.mkdir()
        os.write(master, b"cd child; printf '__DIR_%s__\\n' READY\n")
        self.read_until(master, b"__DIR_READY__")
        result = subprocess.run([str(self.binary), "info", session],
                                env=self.environment, text=True,
                                capture_output=True, timeout=5, check=True)
        self.assertEqual(Path(result.stdout.strip()).resolve(),
                         child.resolve())

    def test_explicit_stop_and_no_restart_of_finished_session(self):
        session, client, _ = self.attach()
        subprocess.run([str(self.binary), "stop", session],
                       env=self.environment, check=True, timeout=5)
        client.wait(timeout=5)
        result = subprocess.run(
            [str(self.binary), "attach", session, self.root.name,
             "touch restarted"], env=self.environment, capture_output=True,
            timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((Path(self.root.name) / "restarted").exists())

    def test_output_continues_while_detached(self):
        _, client, master = self.attach()
        os.write(master, b"sleep 0.2; head -c 9000000 /dev/zero; "
                         b"printf finished > large-output\n")
        client.terminate()
        client.wait(timeout=5)
        deadline = time.monotonic() + 5
        marker = Path(self.root.name) / "large-output"
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(marker.read_text(), "finished")

    def test_invalid_ids_and_unsafe_runtime_directories_rejected(self):
        invalid = subprocess.run([str(self.binary), "stop",
                                  "../../another-user"],
                                 env=self.environment, capture_output=True)
        self.assertNotEqual(invalid.returncode, 0)
        os.chmod(self.root.name, 0o777)
        unsafe = subprocess.run([str(self.binary), "stop",
                                 str(uuid.uuid4())],
                                env=self.environment, capture_output=True)
        self.assertNotEqual(unsafe.returncode, 0)
        os.chmod(self.root.name, 0o700)

    def test_exit_flushes_final_output_before_status(self):
        _, client, master = self.attach()
        os.write(master, b"sleep 0.1; printf '__FINAL_%s__\\n' OUTPUT; "
                         b"exit 7\n")
        self.read_until(master, b"__FINAL_OUTPUT__")
        self.assertEqual(client.wait(timeout=5), 7)


if __name__ == "__main__":
    unittest.main()
