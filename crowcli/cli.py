"""crow-cli command line: a tmux/herdr-like terminal program built only
from Crow's reverse agent + its removed persistent-session runtime."""
import argparse
import getpass
import hashlib
import json
import os
import random
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

from . import __version__, daemon as daemon_mod, registry as registry_mod
from . import clipimage, reverse as reverse_mod, tools as tools_mod
from .client import Client

ENTRY = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "crow")


def eprint(*args):
    print(*args, file=sys.stderr)


# --------------------------------------------------------------- registry
def with_registry(fn):
    reg = registry_mod.Registry()
    with reg.locked():
        return fn(reg)


def get_session(reg, name):
    session = reg.find(name)
    if session is None:
        raise SystemExit("crow-cli: no session %r" % (name or "(last)"))
    return session


def pane_root(window, pane):
    meta = pane.get("meta", {})
    return meta.get("session_root") or window.get("meta", {}).get(
        "session_root")


def pane_status(window, pane):
    meta = pane.get("meta", {}) or window.get("meta", {})
    if meta.get("remote_host"):
        return "remote"
    root = pane_root(window, pane)
    code = daemon_mod.has_exited(pane["id"], root=root)
    if code is not None:
        return "exited(%d)" % code
    return "running" if daemon_mod.is_alive(pane["id"], root=root) \
        else "dead"


def spawn_pane(window, pane):
    meta = pane.get("meta", {}) or window.get("meta", {})
    if meta.get("remote_host"):
        return  # spawned on the server at reverse-agent creation time
    env = {"PROMPT_EOL_MARK": ""}
    # zsh's prompt_sp would otherwise stamp an inverse '%' whenever a
    # prompt follows output, and the mark survives as a stray line when
    # replay is rendered at a different width than it was recorded at.
    server_home = meta.get("server_home")
    if server_home:
        env["HOME"] = server_home
    daemon_mod.spawn(pane["id"], pane["cwd"], pane.get("command"),
                     root=pane_root(window, pane), env=env or None)


def spawn_window(window):
    for pane in registry_mod.window_panes(window):
        spawn_pane(window, pane)


def stop_pane(window, pane):
    meta = pane.get("meta", {}) or window.get("meta", {})
    if meta.get("remote_host"):
        remote_exec(remote_record(meta),
                    ["~/.crow-cli/bin/crow-session", "stop", pane["id"]],
                    check=False, timeout=15)
        return
    daemon_mod.stop(pane["id"], root=pane_root(window, pane))


def stop_window(window):
    for pane in registry_mod.window_panes(window):
        stop_pane(window, pane)


# ------------------------------------------------------------------- ssh
def find_host(reg, ref):
    for host in reg.data.get("hosts", []):
        if host["name"] == ref:
            return host
    return None


def host_record(ref, reg=None):
    """A saved host name -> its record; anything else is a raw address."""
    if isinstance(ref, dict):
        return ref
    if reg is not None:
        saved = find_host(reg, ref)
        if saved:
            return saved
    return {"name": ref, "address": ref, "port": None, "identity": None}


def remote_record(meta):
    """ssh target of a remote reverse window, with its saved port/key."""
    return {"name": meta.get("remote_host_name") or meta["remote_host"],
            "address": meta["remote_host"],
            "port": meta.get("remote_ssh_port"),
            "identity": meta.get("remote_identity")}


def ssh_args_for(record):
    record = host_record(record)
    args = ["ssh", "-o", "ConnectTimeout=10"]
    if record.get("port"):
        args += ["-p", str(record["port"])]
    if record.get("identity"):
        args += ["-i", os.path.expanduser(record["identity"])]
    args.append(record["address"])
    return args


def ssh_base(host):
    return ssh_args_for(host)


def ssh_tunnel_args(record, forwards):
    """ssh -N with saved port/identity, for -R/-L tunnels."""
    record = host_record(record)
    args = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=10"]
    if record.get("port"):
        args += ["-p", str(record["port"])]
    if record.get("identity"):
        args += ["-i", os.path.expanduser(record["identity"])]
    args += list(forwards)
    args.append(record["address"])
    return args


def _remote_quote(arg):
    """Quote one remote-shell word, keeping a leading ~/ expandable:
    shlex.quote('~/x') would freeze the tilde inside single quotes and
    the remote shell would look for a literal '~' directory."""
    if arg == "~":
        return "$HOME"
    if arg.startswith("~/"):
        return "$HOME/" + shlex.quote(arg[2:])
    return shlex.quote(arg)


def remote_exec(host, argv, check=True, timeout=30, stdin_data=None):
    command = " ".join(_remote_quote(a) for a in argv)
    # Never hand ssh the user's terminal: remote `crow-session attach`
    # stays attached until its stdin closes, so a tty stdin hangs here.
    stdin = {"input": stdin_data} if stdin_data is not None \
        else {"stdin": subprocess.DEVNULL}
    return subprocess.run(ssh_base(host) + [command], check=check,
                          timeout=timeout, capture_output=True, text=True,
                          **stdin)


def remote_install(host):
    """Copy the runtime to ~/.crow-cli on the server and build the daemon
    there with cc — Crow's PersistentTerminal.install shape, over ssh."""
    repo = os.path.dirname(ENTRY)
    remote_exec(host, ["mkdir", "-p", "~/.crow-cli/lib", "~/.crow-cli/bin"])
    tar = subprocess.Popen(["tar", "cf", "-", "-C", repo, "crow", "crowcli",
                            "src/crow-session.c"], stdout=subprocess.PIPE)
    subprocess.run(ssh_base(host) + ["tar xf - -C ~/.crow-cli/lib"],
                   stdin=tar.stdout, check=True)
    tar.wait()
    remote_exec(host, ["sh", "-c",
                       "cc -std=c11 -O2 \"$HOME/.crow-cli/lib/src/"
                       "crow-session.c\" -o \"$HOME/.crow-cli/bin/"
                       "crow-session.tmp\" && chmod 700 \"$HOME/.crow-cli/"
                       "bin/crow-session.tmp\" && mv \"$HOME/.crow-cli/bin/"
                       "crow-session.tmp\" \"$HOME/.crow-cli/bin/"
                       "crow-session\""], timeout=120)


# ---------------------------------------------------------------- bridge
def bridge_state_path(root):
    digest = hashlib.sha256(
        tools_mod.canonical(root).encode()).hexdigest()[:16]
    base = os.path.join(registry_mod.home(), "bridges")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, digest + ".json")


def ensure_bridge(root):
    """Start (or reuse) the client-side crow_client bridge for a root."""
    path = bridge_state_path(root)
    if os.path.exists(path):
        try:
            with open(path) as fh:
                state = json.load(fh)
            os.kill(state["pid"], 0)
            return state
        except (ValueError, KeyError, ProcessLookupError, PermissionError):
            pass
    token = secrets.token_hex(16)
    proc = subprocess.Popen(
        [sys.executable, ENTRY, "agent-bridge", "--root", root,
         "--token", token],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, text=True)
    line = proc.stdout.readline()
    port = json.loads(line)["port"]
    state = {"pid": proc.pid, "port": port, "token": token,
             "root": tools_mod.canonical(root)}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return state


def any_bridge_alive():
    base = os.path.join(registry_mod.home(), "bridges")
    if not os.path.isdir(base):
        return False
    for name in os.listdir(base):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(base, name)) as fh:
                state = json.load(fh)
            os.kill(state["pid"], 0)
            return True
        except (ValueError, KeyError, OSError):
            continue
    return False


# ---------------------------------------------------------------- attach
def pick_session(reg):
    sessions = reg.data["sessions"]
    if not sessions:
        return None
    recent = reg.find()["name"]
    if len(sessions) == 1 or not sys.stdin.isatty():
        return recent
    for i, s in enumerate(sessions):
        print("%s%d) %s (%d windows, %s)" % (
            "*" if s["name"] == recent else " ", i, s["name"],
            len(s["windows"]), s["kind"]))
    try:
        choice = input("session [%s]: " % recent).strip()
    except EOFError:
        return recent
    if choice.isdigit() and int(choice) < len(sessions):
        return sessions[int(choice)]["name"]
    return choice or recent


def ensure_reverse_forwards(session, window):
    """For remote reverse sessions: open ssh -R (MCP bridge) and ssh -L
    (PTY socket) for the duration of the attach. Returns processes."""
    meta = window.get("meta", {})
    host = meta.get("remote_host")
    if not host:
        if session["kind"].startswith("reverse"):
            ensure_bridge(meta["client_root"])
        return []
    bridge = ensure_bridge(meta["client_root"])
    if bridge["token"] != meta.get("token"):
        eprint("crow-cli: bridge changed; agent tools will reconnect")
    record = remote_record(meta)
    procs = []
    remote_port = meta["bridge_remote_port"]
    tunnel = subprocess.Popen(
        ssh_tunnel_args(record, ["-R", "127.0.0.1:%d:127.0.0.1:%d"
                                 % (remote_port, bridge["port"])]),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    procs.append(tunnel)
    forward_dir = os.path.join(registry_mod.home(), "forwards")
    os.makedirs(forward_dir, mode=0o700, exist_ok=True)
    pane = registry_mod.active_pane(window)
    local_sock = os.path.join(forward_dir, pane["id"] + ".sock")
    if os.path.exists(local_sock):
        os.unlink(local_sock)
    remote_sock = meta["remote_socket"]
    forward = subprocess.Popen(
        ssh_tunnel_args(record, ["-L", "%s:%s" % (local_sock,
                                                  remote_sock)]),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    procs.append(forward)
    window["meta"]["session_root"] = forward_dir
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.exists(local_sock):
            break
        if forward.poll() is not None:
            raise SystemExit("crow-cli: ssh 소켓 포워드 실패 — "
                             "호스트 연결 상태를 확인하세요")
        time.sleep(0.05)
    return procs


def image_target(pane_meta, window_meta):
    """Where a Ctrl-V clipboard image goes for this pane: the server of a
    remote reverse agent or ssh session. Local panes get None, so their
    programs (claude, codex…) read the Mac clipboard themselves."""
    meta = pane_meta or window_meta
    if meta.get("remote_host"):
        record = remote_record(meta)
    elif meta.get("ssh_host"):
        record = host_record(meta.get("ssh_host_name") or meta["ssh_host"],
                             registry_mod.load_registry())
    else:
        return None
    args = ssh_args_for(record)
    return lambda data: clipimage.upload(args[:1] + ["-o", "BatchMode=yes"]
                                         + args[1:], data)


def run_attach(name):
    forwards = []
    remote_drops = 0
    notice = {"text": None}  # last failure, shown in the sidebar
    try:
        while True:
            reg = registry_mod.load_registry()
            session = reg.find(name) if name else reg.find()
            if session is None:
                eprint("crow-cli: no sessions. Create one: crow new")
                return 1
            name = session["name"]
            window = registry_mod.Registry.active_window(session)
            if window is None:
                eprint("crow-cli: session %s has no windows" % name)
                return 1
            spawn_window(window)
            for proc in forwards:
                proc.terminate()
            forwards = ensure_reverse_forwards(session, window)

            def mark_attached(reg):
                found = reg.find(name)
                if found:
                    found["last_attached"] = time.time()
            with_registry(mark_attached)

            def status_text(session=session, current=window):
                detail = session["kind"]
                provider = current.get("meta", {}).get("provider")
                if provider:
                    detail += "/" + provider
                return " %s | %s " % (session["name"], detail)

            def window_tabs(session=session, current=window):
                return [(w["name"], w["id"] == current["id"])
                        for w in session["windows"]]

            def explorer_root(pane_id, window=window):
                pane = registry_mod.pane_by_id(window, pane_id) or \
                    registry_mod.active_pane(window)
                meta = pane.get("meta", {}) or window.get("meta", {})
                if meta.get("client_root"):
                    return meta["client_root"]
                return daemon_mod.info(pane["id"],
                                       root=pane_root(window, pane)) \
                    or pane["cwd"]

            def reverse_data():
                fresh = registry_mod.load_registry()
                hosts = sorted(fresh.data.get("hosts", []),
                               key=lambda h: h.get("last_used") or 0,
                               reverse=True)
                return {"hosts": hosts}

            def sidebar_data(current=name):
                fresh = registry_mod.load_registry()
                sessions = []
                for item in fresh.data["sessions"]:
                    panes = registry_mod.window_panes(
                        registry_mod.Registry.active_window(item)
                        or item["windows"][0]) if item["windows"] else []
                    meta = {}
                    if panes:
                        meta = panes[0].get("meta", {}) or \
                            item["windows"][0].get("meta", {})
                    alive = any(
                        daemon_mod.is_alive(
                            pane["id"],
                            root=pane_root(item["windows"][0], pane))
                        for pane in panes) if panes else False
                    if item["kind"].startswith("reverse"):
                        if meta.get("remote_host"):
                            tunnel_dead = item["name"] == current and any(
                                proc.poll() is not None for proc in forwards)
                            status = "연결 실패" if tunnel_dead else \
                                "연결됨" if item["name"] == current \
                                else "재접속 대기"
                        elif not alive:
                            status = "종료"
                        elif any_bridge_alive():
                            status = "연결됨"
                        else:
                            status = "브리지 끊김"
                    else:
                        status = "실행 중" if alive else "종료"
                    sessions.append({
                        "name": item["name"], "kind": item["kind"],
                        "status": status,
                        "current": item["name"] == current})
                return {"sessions": sessions,
                        "hosts": fresh.data.get("hosts", []),
                        "keys": key_entries(fresh, fingerprints=False)}

            def delete_key(key, files):
                try:
                    _key, deleted = remove_key(key["name"], files)
                except ValueError as error:
                    return str(error)
                return "%s: %s" % (key["name"], "파일까지 삭제함" if deleted
                                   else "등록 해제함")

            client = Client(object(), status_provider=status_text,
                            explorer_root_provider=explorer_root,
                            reverse_data_provider=reverse_data,
                            sidebar_data_provider=sidebar_data,
                            notice_provider=lambda: notice["text"],
                            windows_provider=window_tabs,
                            host_form={"keys": host_form_keys,
                                       "derive": derive_host_name,
                                       "save": host_form_save},
                            image_target=image_target,
                            key_actions={"list": key_entries,
                                         "copy": copy_public_key,
                                         "delete": delete_key})
            result = client.attach(name, window)

            def save_active_pane(reg, pane_id=client.last_active_pane_id):
                found = reg.find(name)
                if not found:
                    return
                win = registry_mod.Registry.active_window(found)
                if win and pane_id:
                    panes = registry_mod.window_panes(win)
                    for i, pane in enumerate(panes):
                        if pane["id"] == pane_id:
                            win["active_pane"] = i
            with_registry(save_active_pane)
            for proc in forwards:
                proc.terminate()
            forwards = []
            if result.action != "dead":
                remote_drops = 0

            if result.action == "detach":
                print("\n[crow-cli] detached from %s:%s (still running)"
                      % (name, window["name"]))
                return 0
            if result.action == "dead":
                pane_id, code = result.arg if isinstance(result.arg, tuple) \
                    else (window["id"], result.arg)
                if code is None and window.get("meta", {}).get("remote_host"):
                    # Only the ssh forward dropped; the agent still runs
                    # on the server, so keep the session for re-attach.
                    remote_drops += 1
                    notice["text"] = "원격 연결 끊김: %s" % name
                    if remote_drops < 3:
                        time.sleep(1)
                        continue
                    print("\n[crow-cli] %s 원격 연결이 계속 끊깁니다 — 서버의 "
                          "에이전트는 그대로 있어요. 재접속: crow attach -t %s"
                          % (name, name))
                    return 1
                print("\n[crow-cli] pane exited (code %s)" % (code,))

                def drop(reg):
                    found = reg.find(name)
                    if not found:
                        return
                    win = registry_mod.Registry.active_window(found)
                    if not win:
                        return
                    reg.remove_pane(win, pane_id)
                    if not win["panes"]:
                        reg.remove_window(found, win["id"])
                    if not found["windows"]:
                        reg.remove_session(name)
                with_registry(drop)
                reg = registry_mod.load_registry()
                if reg.find(name) is None:
                    return int(code or 0) if code else 0
                continue
            if result.action == "open-file":
                path = result.arg
                editor = os.environ.get("EDITOR") or "vi"

                def open_file(reg, path=path, editor=editor):
                    found = reg.find(name)
                    win = registry_mod.Registry.active_window(found)
                    target = registry_mod.active_pane(win)
                    pane = reg.add_pane(
                        win, cwd=os.path.dirname(path),
                        command="%s %s" % (editor, shlex.quote(path)),
                        direction="v", target_id=target["id"],
                        meta=dict(target.get("meta", {})))
                    spawn_pane(win, pane)
                with_registry(open_file)
                continue
            if result.action == "switch-session":
                name = result.arg
                continue
            if result.action == "ssh-connect":
                record = host_record(result.arg)
                name = with_registry(
                    lambda reg: ensure_ssh_session(reg, record))
                continue
            if result.action == "host-check":
                record = host_record(result.arg)
                print("[crow-cli] 키·연결 확인: %s" % record["address"])
                check_host(record)
                try:
                    input("[Enter를 누르면 돌아갑니다] ")
                except EOFError:
                    pass
                continue
            if result.action in ("key-generate", "key-rename",
                                 "key-install"):
                key_screen(result.action, result.arg, notice)
                continue
            if result.action == "host-install-key":
                record = host_record(result.arg)
                reg = registry_mod.load_registry()
                key = find_key(reg, record.get("identity")) \
                    if record.get("identity") else None
                if key is None:
                    keys = reg.data.get("keys", [])
                    key = find_key(reg, keys[0]["name"]) if keys else None
                if key is None:
                    print("[crow-cli] 설치할 키가 없습니다. 먼저 만드세요: "
                          "crow key add work")
                else:
                    print("[crow-cli] %s 공개키를 %s에 설치합니다 "
                          "(서버 비밀번호를 한 번 물을 수 있어요)"
                          % (key["name"], record["address"]))
                    if install_key(record, key):
                        if record.get("identity") != key["path"] and \
                                find_host(reg, record.get("name")):

                            def bind(reg, path=key["path"],
                                     hname=record.get("name")):
                                saved = find_host(reg, hname)
                                if saved:
                                    saved["identity"] = path
                            with_registry(bind)
                        notice["text"] = None
                    else:
                        notice["text"] = "키 설치 실패: %s" \
                            % record["address"]
                try:
                    input("[Enter를 누르면 돌아갑니다] ")
                except EOFError:
                    pass
                continue
            if result.action == "reverse-start":
                payload = result.arg
                existing = find_reverse_session(
                    registry_mod.load_registry(), payload["provider"],
                    payload["client_root"], payload["host"],
                    payload["local"])
                if existing:
                    name = existing
                    continue
                print("[crow-cli] 리버스 에이전트 시작 중… (호스트: %s, "
                      "작업 폴더: %s)"
                      % ("로컬 샌드박스" if payload["local"] else
                         (payload["host"] or {}).get("address", "?"),
                         payload["client_root"]))
                try:
                    name = launch_reverse_agent(
                        payload["provider"], payload["client_root"],
                        host_ref=payload["host"], local=payload["local"])
                    notice["text"] = None
                except (SystemExit, subprocess.SubprocessError, OSError,
                        ValueError) as error:
                    detail = error.code if isinstance(error, SystemExit) \
                        else str(error)
                    notice["text"] = "리버스 시작 실패: %s" % detail
                    eprint("crow-cli: ✗ %s" % notice["text"])
                    try:
                        input("[Enter를 누르면 돌아갑니다] ")
                    except EOFError:
                        pass
                continue
            if result.action in ("split-v", "split-h"):
                direction = "v" if result.action == "split-v" else "h"
                target_pane = registry_mod.active_pane(window)
                cwd = daemon_mod.info(
                    target_pane["id"],
                    root=pane_root(window, target_pane)) or target_pane["cwd"]

                def split(reg, direction=direction, cwd=cwd):
                    found = reg.find(name)
                    win = registry_mod.Registry.active_window(found)
                    target = registry_mod.active_pane(win)
                    pane = reg.add_pane(win, cwd=cwd, direction=direction,
                                        target_id=target["id"],
                                        meta=dict(target.get("meta", {})))
                    spawn_pane(win, pane)
                with_registry(split)
                continue
            if result.action == "zoom":
                def zoom(reg):
                    found = reg.find(name)
                    win = registry_mod.Registry.active_window(found)
                    active = registry_mod.active_pane(win)
                    win["zoomed"] = None if win.get("zoomed") else \
                        (active["id"] if active else None)
                with_registry(zoom)
                continue
            if result.action == "switch":
                def move(reg):
                    found = reg.find(name)
                    if not found or not found["windows"]:
                        return
                    if isinstance(result.arg, tuple):
                        found["active"] = result.arg[1] % len(found["windows"])
                    else:
                        found["active"] = (found.get("active", 0)
                                           + result.arg) % len(found["windows"])
                with_registry(move)
                continue
            if result.action == "new":
                target_pane = registry_mod.active_pane(window)
                cwd = daemon_mod.info(
                    target_pane["id"],
                    root=pane_root(window, target_pane)) or target_pane["cwd"]

                def create(reg):
                    found = reg.find(name)
                    win = reg.add_window(found, cwd=cwd)
                    spawn_window(win)
                with_registry(create)
                continue
            if result.action in ("rename-window", "rename-session"):
                try:
                    new_name = input("new name: ").strip()
                except EOFError:
                    new_name = ""
                if new_name:
                    registry_mod.validate_name(new_name)

                    def rename(reg):
                        found = reg.find(name)
                        if result.action == "rename-window":
                            win = registry_mod.Registry.active_window(found)
                            win["name"] = new_name
                        else:
                            found["name"] = new_name
                    with_registry(rename)
                    if result.action == "rename-session":
                        name = new_name
                continue
            if result.action == "kill-pane":
                pane = registry_mod.pane_by_id(window, result.arg) or \
                    registry_mod.active_pane(window)
                try:
                    answer = input("kill pane %s? [y/N] "
                                   % pane["name"]).strip().lower()
                except EOFError:
                    answer = ""
                if answer == "y":
                    stop_pane(window, pane)

                    def drop2(reg):
                        found = reg.find(name)
                        if not found:
                            return
                        win = registry_mod.Registry.active_window(found)
                        reg.remove_pane(win, pane["id"])
                        if not win["panes"]:
                            reg.remove_window(found, win["id"])
                        if not found["windows"]:
                            reg.remove_session(name)
                    with_registry(drop2)
                    if registry_mod.load_registry().find(name) is None:
                        return 0
                continue
            if result.action == "choose":
                chosen = with_registry(pick_session)
                if chosen and chosen != name:
                    name = chosen
                continue
            return 0
    finally:
        for proc in forwards:
            proc.terminate()


# -------------------------------------------------------------- commands
def parse_target(target):
    if not target:
        return None, None
    if ":" in target:
        name, _, win = target.partition(":")
        return name or None, win
    return target, None


def find_window(session, selector):
    if selector is None:
        return registry_mod.Registry.active_window(session)
    if selector.isdigit():
        idx = int(selector)
        if 0 <= idx < len(session["windows"]):
            return session["windows"][idx]
        return None
    for win in session["windows"]:
        if win["name"] == selector or win["id"].startswith(selector):
            return win
    return None


def find_pane(session, selector):
    """selector: [window][.pane] — indexes or names, default active."""
    win_selector, dot, pane_selector = (selector or "").partition(".")
    win = find_window(session, win_selector or None)
    if win is None:
        return None, None
    panes = registry_mod.window_panes(win)
    if not dot or not pane_selector:
        return win, registry_mod.active_pane(win)
    if pane_selector.isdigit():
        idx = int(pane_selector)
        return win, panes[idx] if 0 <= idx < len(panes) else None
    for pane in panes:
        if pane["name"] == pane_selector or pane["id"].startswith(
                pane_selector):
            return win, pane
    return win, None


def cmd_new(args):
    name = args.session or "main"
    created = []

    def create(reg):
        session = reg.find(name)
        if session is None:
            session = reg.create_session(name)
        words = list(args.command or [])
        if words and words[0] == "--":
            words = words[1:]
        command = " ".join(words) if words else None
        win = reg.add_window(session, name=args.window_name,
                             cwd=os.path.abspath(args.directory)
                             if args.directory else os.getcwd(),
                             command=command)
        spawn_window(win)
        created.append(win)
    with_registry(create)
    if args.detached:
        print("%s:%s" % (name, created[0]["name"]))
        return 0
    return run_attach(name)


def cmd_ls(args):
    reg = registry_mod.load_registry()
    if not reg.data["sessions"]:
        print("no sessions (crow new to create one)")
        return 0
    for s in reg.data["sessions"]:
        print("%s: %d window(s) [%s] created %s" % (
            s["name"], len(s["windows"]), s["kind"],
            time.strftime("%H:%M", time.localtime(s["created_at"]))))
        for i, w in enumerate(s["windows"]):
            mark = "*" if i == s.get("active", 0) else " "
            panes = registry_mod.window_panes(w)
            print("  %s %d: %s [%d pane(s)]" % (mark, i, w["name"],
                                                len(panes)))
            for j, pane in enumerate(panes):
                pmark = "*" if j == w.get("active_pane", 0) else " "
                print("      %s %d.%d: %s (%s) %s" % (
                    pmark, i, j, pane["name"], pane_status(w, pane),
                    pane["cwd"]))
    return 0


def cmd_list_windows(args):
    reg = registry_mod.load_registry()
    session = get_session(reg, args.target)
    for i, w in enumerate(session["windows"]):
        panes = registry_mod.window_panes(w)
        print("%d: %s [%d pane(s)]" % (i, w["name"], len(panes)))
        for j, pane in enumerate(panes):
            print("  %d.%d: %s (%s) %s" % (i, j, pane["name"],
                                           pane_status(w, pane),
                                           pane["cwd"]))
    return 0


def cmd_new_window(args):
    name, _ = parse_target(args.target)

    def create(reg):
        session = get_session(reg, name)
        words = list(args.command or [])
        if words and words[0] == "--":
            words = words[1:]
        command = " ".join(words) if words else None
        win = reg.add_window(session, name=args.window_name,
                             cwd=os.path.abspath(args.directory)
                             if args.directory else os.getcwd(),
                             command=command)
        spawn_window(win)
        print("%s:%s" % (session["name"], win["name"]))
    with_registry(create)
    return 0


def cmd_kill(args, window=False, pane=False):
    name, selector = parse_target(args.target)

    def kill(reg):
        session = get_session(reg, name)
        if pane:
            win, target_pane = find_pane(session, selector)
            if target_pane is None:
                raise SystemExit("crow-cli: no such pane")
            stop_pane(win, target_pane)
            reg.remove_pane(win, target_pane["id"])
            if not win["panes"]:
                reg.remove_window(session, win["id"])
            if not session["windows"]:
                reg.remove_session(session["name"])
            return
        if window:
            win = find_window(session, selector)
            if win is None:
                raise SystemExit("crow-cli: no such window")
            stop_window(win)
            reg.remove_window(session, win["id"])
            if not session["windows"]:
                reg.remove_session(session["name"])
        else:
            for win in list(session["windows"]):
                stop_window(win)
            reg.remove_session(session["name"])
    with_registry(kill)
    return 0


def cmd_select(args):
    name, selector = parse_target(args.target)

    def select(reg):
        session = get_session(reg, name)
        win = find_window(session, selector)
        if win is None:
            raise SystemExit("crow-cli: no such window")
        session["active"] = session["windows"].index(win)
    with_registry(select)
    return 0


def cmd_capture(args):
    name, selector = parse_target(args.target)
    reg = registry_mod.load_registry()
    session = get_session(reg, name)
    win, pane = find_pane(session, selector)
    if pane is None:
        raise SystemExit("crow-cli: no such pane")
    if (pane.get("meta", {}) or win.get("meta", {})).get("remote_host"):
        raise SystemExit("crow-cli: capture is local-only for now; "
                         "attach to the remote window instead")
    data = daemon_mod.capture(pane["id"], root=pane_root(win, pane))
    if data is None:
        raise SystemExit("crow-cli: window is not running")
    if data.startswith(b"\x1bc"):
        data = data[2:]
    sys.stdout.buffer.write(data)
    if data and not data.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    return 0


def cmd_info(args):
    name, selector = parse_target(args.target)
    reg = registry_mod.load_registry()
    session = get_session(reg, name)
    win, pane = find_pane(session, selector)
    if pane is None:
        raise SystemExit("crow-cli: no such pane")
    meta = pane.get("meta", {}) or win.get("meta", {})
    if meta.get("remote_host"):
        result = remote_exec(remote_record(meta),
                             ["~/.crow-cli/bin/crow-session", "info",
                              pane["id"]], check=False)
        print(result.stdout.strip() or "(unavailable)")
        return 0
    cwd = daemon_mod.info(pane["id"], root=pane_root(win, pane))
    print(cwd or "(not running)")
    return 0 if cwd else 1


def cmd_split_window(args):
    name, selector = parse_target(args.target)
    direction = "h" if args.vertical else "v"

    def split(reg):
        session = get_session(reg, name)
        win, target = find_pane(session, selector)
        if target is None:
            raise SystemExit("crow-cli: no such pane")
        words = list(args.command or [])
        if words and words[0] == "--":
            words = words[1:]
        cwd = daemon_mod.info(target["id"],
                              root=pane_root(win, target)) or target["cwd"]
        if args.directory:
            cwd = os.path.abspath(args.directory)
        pane = reg.add_pane(win, cwd=cwd,
                            command=" ".join(words) if words else None,
                            direction=direction, target_id=target["id"],
                            meta=dict(target.get("meta", {})))
        spawn_pane(win, pane)
        print("%s:%s.%d split %s" % (session["name"], win["name"],
                                     registry_mod.window_panes(
                                         win).index(pane),
                                     "left/right" if direction == "v"
                                     else "top/bottom"))
    with_registry(split)
    return 0


def cmd_select_pane(args):
    name, selector = parse_target(args.target)

    def select(reg):
        session = get_session(reg, name)
        win, pane = find_pane(session, selector)
        if pane is None:
            raise SystemExit("crow-cli: no such pane")
        session["active"] = session["windows"].index(win)
        win["active_pane"] = registry_mod.window_panes(win).index(pane)
    with_registry(select)
    return 0


def cmd_rename(args):
    name, selector = parse_target(args.target)
    registry_mod.validate_name(args.new_name)

    def rename(reg):
        session = get_session(reg, name)
        if args.window:
            win = find_window(session, selector)
            win["name"] = args.new_name
        else:
            session["name"] = args.new_name
    with_registry(rename)
    return 0


# ------------------------------------------------------------ host admin
def cmd_hosts(args):
    reg = registry_mod.load_registry()
    hosts = reg.data.get("hosts", [])
    if not hosts:
        print("no saved hosts. Add one: crow host add NAME "
              "--address user@server")
        return 0
    for host in hosts:
        extra = ""
        if host.get("port"):
            extra += " port=%s" % host["port"]
        extra += " key=%s" % (host.get("identity") or "기본(자동)")
        if host.get("last_used"):
            extra += " last=%s" % time.strftime(
                "%m-%d %H:%M", time.localtime(host["last_used"]))
        print("%s: %s%s" % (host["name"], host["address"], extra))
    return 0


def derive_host_name(address):
    host = address.rpartition("@")[2] or address
    if re.match(r"^[0-9.]+$", host):
        return host  # an IP's first octet says nothing
    name = re.sub(r"[^A-Za-z0-9_.\-]+", "-", host.split(".")[0]).strip("-")
    return name or "host"


def host_form_keys():
    """(label, path) choices for the UI host form: managed keys first."""
    reg = registry_mod.load_registry()
    choices, seen = [], set()
    for key in reg.data.get("keys", []):
        path = os.path.expanduser(key["path"])
        seen.add(path)
        choices.append((key["name"], path))
    for key in discover_system_keys():
        if key["path"] not in seen:
            choices.append(("~/.ssh/" + key["name"], key["path"]))
    return choices


def host_form_save(address, port_text, identity):
    """Save a host from the UI form; ValueError carries the message."""
    port = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError("포트는 1~65535 사이 숫자여야 해요")
        port = int(port_text)
    name = derive_host_name(address)
    return with_registry(
        lambda reg: save_host(reg, name, address, port, identity))


def save_host(reg, name, address, port=None, identity=None):
    registry_mod.validate_name(name)
    existing = find_host(reg, name)
    record = {"name": name, "address": address, "port": port,
              "identity": identity,
              "last_used": existing.get("last_used") if existing else None}
    if existing:
        reg.data["hosts"][reg.data["hosts"].index(existing)] = record
    else:
        reg.data.setdefault("hosts", []).append(record)
    return record


def cmd_host_add(args):
    name, address = args.name, args.address
    if name and not address and ("@" in name or "." in name):
        # `crow host add user@host` — one field is enough.
        address, name = name, None
    if not address:
        try:
            address = input("SSH 주소 (user@host 또는 host만): ").strip()
        except EOFError:
            address = ""
    if not address:
        raise SystemExit("crow-cli: SSH 주소가 필요합니다 "
                         "(user@host 또는 host)")
    name = name or derive_host_name(address)

    identity = args.identity
    if args.key:
        key = with_registry(lambda reg: find_key(reg, args.key))
        if key is None:
            raise SystemExit("crow-cli: no such key %r (crow keys)"
                             % args.key)
        identity = key["path"]

    def add(reg):
        save_host(reg, name, address, args.port, identity)
        print("%s: %s (키: %s)" % (
            name, address,
            identity or "기본 키 자동 탐색 ~/.ssh/id_*"))
    with_registry(add)
    return 0


def cmd_host_rm(args):
    def remove(reg):
        hosts = reg.data.get("hosts", [])
        reg.data["hosts"] = [h for h in hosts if h["name"] != args.name]
        if len(reg.data["hosts"]) == len(hosts):
            raise SystemExit("crow-cli: no saved host %r" % args.name)
    with_registry(remove)
    print("removed %s" % args.name)
    return 0


# -------------------------------------------------------------- ssh keys
def key_fingerprint(path):
    """(display dict) from ssh-keygen -lf; works on the public blob, so
    passphrase-protected keys still report a fingerprint (Crow-style)."""
    try:
        out = subprocess.run(["ssh-keygen", "-lf",
                              os.path.expanduser(path)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.split()
    if len(parts) < 4:
        return None
    return {"bits": parts[0], "sha256": parts[1],
            "type": parts[-1].strip("()")}


def discover_system_keys(fingerprints=True):
    """Private keys sitting in ~/.ssh (Crow's automatic discovery).
    fingerprints=False skips ssh-keygen (cheap enough for the sidebar)."""
    base = os.path.expanduser("~/.ssh")
    found = []
    if not os.path.isdir(base):
        return found
    for name in sorted(os.listdir(base)):
        if name.endswith(".pub"):
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                head = fh.readline()
        except OSError:
            continue
        if b"PRIVATE KEY" not in head:
            continue
        if not fingerprints:
            found.append({"name": name, "path": path})
            continue
        info = key_fingerprint(path)
        if info:
            found.append(dict({"name": name, "path": path}, **info))
    return found


def key_entries(reg=None, fingerprints=True):
    """Managed keys, then ~/.ssh keys not already managed; each with
    origin, managed flag and the saved hosts that use it."""
    reg = reg or registry_mod.load_registry()
    hosts = reg.data.get("hosts", [])

    def users(path):
        return [h["name"] for h in hosts if h.get("identity")
                and os.path.expanduser(h["identity"]) == path]
    entries, seen = [], set()
    for key in reg.data.get("keys", []):
        path = os.path.expanduser(key["path"])
        seen.add(path)
        info = key_fingerprint(path) or {} if fingerprints else {}
        entries.append(dict(info, name=key["name"], path=path,
                            managed=True, imported=key.get("imported"),
                            origin="관리됨", hosts=users(path)))
    for key in discover_system_keys(fingerprints):
        if key["path"] not in seen:
            entries.append(dict(key, managed=False, origin="~/.ssh",
                                hosts=users(key["path"])))
    return entries


def public_key_text(key):
    """The one-line public key, or None (passphrase key without .pub)."""
    pub_path = key["path"] + ".pub"
    if os.path.isfile(pub_path):
        with open(pub_path) as fh:
            return fh.read().strip()
    out = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", key["path"]],
                         capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    if out.returncode == 0:
        return "%s %s" % (out.stdout.strip(), key["name"])
    return None


def copy_public_key(key):
    """Public key -> macOS clipboard; returns a message for the UI."""
    text = public_key_text(key)
    if text is None:
        return "공개키를 읽을 수 없어요 (%s.pub 없음)" % key["name"]
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True,
                       timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "클립보드 복사 실패 (pbcopy 없음)"
    return "%s 공개키를 클립보드에 복사했어요" % key["name"]


def clean_key_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name or "").strip("-")


def generate_key(name, passphrase="", key_type="ed25519"):
    """ssh-keygen ~/.ssh/crow_NAME and register it; returns the record."""
    name = clean_key_name(name)
    if not name:
        raise ValueError("키 이름이 필요합니다")
    if find_key(registry_mod.load_registry(), name):
        raise ValueError("이미 있는 키 이름입니다: %s" % name)
    ssh_dir_path = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir_path, mode=0o700, exist_ok=True)
    path = os.path.join(ssh_dir_path, "crow_" + name)
    if os.path.exists(path):
        raise ValueError("이미 있습니다: %s" % path)
    gen = ["ssh-keygen", "-t", key_type, "-f", path, "-N", passphrase,
           "-C", name + "@crow-cli"]
    if key_type == "rsa":
        gen[3:3] = ["-b", "4096"]
    result = subprocess.run(gen, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise ValueError("키 생성 실패: %s" % result.stderr.strip())
    record = {"name": name, "path": path, "imported": False,
              "created_at": time.time()}
    with_registry(lambda reg: reg.data.setdefault("keys", []).append(record))
    return record


def rename_key(old, new):
    new = clean_key_name(new)
    if not new:
        raise ValueError("새 이름이 필요합니다")

    def rename(reg):
        keys = reg.data.get("keys", [])
        if any(k["name"] == new for k in keys):
            raise ValueError("이미 있는 키 이름입니다: %s" % new)
        for key in keys:
            if key["name"] == old:
                key["name"] = new
                return new
        raise ValueError("관리 중인 키가 아닙니다: %s" % old)
    return with_registry(rename)


def remove_key(name, delete_files=False):
    """Unregister a managed key (and optionally its crow-generated
    files). Keys still used by a saved host are refused."""
    def remove(reg):
        key = next((k for k in reg.data.get("keys", [])
                    if k["name"] == name), None)
        if key is None:
            raise ValueError("관리 중인 키가 아닙니다. ~/.ssh 키는 "
                             "파일로 직접 지우세요")
        path = os.path.expanduser(key["path"])
        users = [h["name"] for h in reg.data.get("hosts", [])
                 if h.get("identity")
                 and os.path.expanduser(h["identity"]) == path]
        if users:
            raise ValueError("%s 호스트가 사용 중인 키입니다. 호스트 키를 "
                             "먼저 바꾸세요" % ", ".join(users))
        reg.data["keys"].remove(key)
        return key
    key = with_registry(remove)
    deleted = False
    if delete_files and not key.get("imported"):
        for target in (key["path"], key["path"] + ".pub"):
            if os.path.exists(target):
                os.unlink(target)
                deleted = True
    return key, deleted


def bind_host_key(host_name, path):
    def bind(reg):
        saved = find_host(reg, host_name)
        if saved:
            saved["identity"] = path
        return bool(saved)
    return with_registry(bind)


def key_screen(action, arg, notice):
    """The UI key actions that need typing (name, passphrase, server
    password) run on the plain terminal between attaches."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()
    try:
        if action == "key-generate":
            print("[crow-cli] 새 SSH 키 (Ed25519, ~/.ssh/crow_<이름>)")
            name = input("키 이름: ").strip()
            passphrase = getpass.getpass("키 암호 [Enter면 없음]: ")
            record = generate_key(name, passphrase)
            info = key_fingerprint(record["path"]) or {}
            print("✓ %s 생성됨 %s" % (record["name"], info.get("sha256", "")))
            print(copy_public_key(dict(record)))
            print("서버에 넣으려면: 사이드바 SSH 키 → %s → 서버에 공개키 설치"
                  % record["name"])
        elif action == "key-rename":
            print("[crow-cli] 키 이름 변경: %s" % arg["name"])
            new = rename_key(arg["name"], input("새 이름: ").strip())
            print("✓ %s → %s" % (arg["name"], new))
        elif action == "key-install":
            key, host = arg
            record = host_record(host)
            print("[crow-cli] %s 공개키를 %s에 설치합니다 (서버 비밀번호를 "
                  "한 번 물을 수 있어요)" % (key["name"], record["address"]))
            if install_key(record, key):
                if record.get("name") and \
                        bind_host_key(record["name"], key["path"]):
                    print("✓ %s 호스트의 키로 지정함" % record["name"])
                notice["text"] = None
            else:
                print("✗ 설치 실패 — 주소/비밀번호를 확인하세요")
                notice["text"] = "키 설치 실패: %s" % record["address"]
    except ValueError as error:
        print("✗ %s" % error)
    except (KeyboardInterrupt, EOFError):
        print("\n취소됨")
    try:
        input("[Enter를 누르면 돌아갑니다] ")
    except (KeyboardInterrupt, EOFError):
        pass


def find_key(reg, ref):
    """A managed key (registry) or a ~/.ssh key, by name or path."""
    if not ref:
        return None
    for key in reg.data.get("keys", []):
        if key["name"] == ref:
            return dict(key, managed=True)
    for key in discover_system_keys():
        if key["name"] == ref:
            return dict(key, managed=False)
    path = os.path.expanduser(ref)
    if os.path.isfile(path):
        info = key_fingerprint(path) or {}
        return dict({"name": os.path.basename(path), "path": path},
                    managed=False, **info)
    return None


def cmd_keys(_args):
    entries = key_entries()
    if not entries:
        print("no ssh keys. Create one: crow key add work")
        return 0
    for key in entries:
        used = " (호스트: %s)" % ", ".join(key["hosts"]) if key["hosts"] else ""
        print("%s: %s %s [%s] %s%s" % (
            key["name"], key.get("type", "?"), key.get("sha256", "?"),
            key["origin"], key["path"], used))
    return 0


def cmd_key_add(args):
    name = clean_key_name(args.name)
    if not name:
        raise SystemExit("crow-cli: 키 이름이 필요합니다")
    if args.file:
        path = os.path.abspath(os.path.expanduser(args.file))
        if not os.path.isfile(path) or not key_fingerprint(path):
            raise SystemExit("crow-cli: OpenSSH 개인키 파일이 아닙니다: %s"
                             % path)
        record = {"name": name, "path": path, "imported": True,
                  "created_at": time.time()}

        def add_imported(reg):
            if find_key(reg, name):
                raise SystemExit("crow-cli: 이미 있는 키 이름입니다")
            reg.data.setdefault("keys", []).append(record)
        with_registry(add_imported)
        print("%s: %s 가져옴" % (name, path))
        return 0
    passphrase = args.passphrase
    if passphrase is None:
        passphrase = getpass.getpass("키 암호 [Enter면 없음]: ") \
            if sys.stdin.isatty() else ""
    try:
        record = generate_key(name, passphrase, args.type)
    except ValueError as error:
        raise SystemExit("crow-cli: %s" % error)
    info = key_fingerprint(record["path"]) or {}
    print("%s: %s %s 생성됨 (%s)" % (name, info.get("type", "?"),
                                     info.get("sha256", ""), record["path"]))
    print("서버에 설치: crow key install %s <호스트>" % name)
    return 0


def cmd_key_show(args):
    reg = registry_mod.load_registry()
    key = find_key(reg, args.name)
    if key is None:
        raise SystemExit("crow-cli: no such key %r (crow keys)" % args.name)
    info = key_fingerprint(key["path"]) or {}
    print("%s: %s %s" % (key["name"], info.get("type", "?"),
                         info.get("sha256", "?")))
    print("개인키: %s" % key["path"])
    text = public_key_text(key)
    print("공개키: " + (text or "(암호가 걸린 키 — %s.pub 파일이 없습니다)"
                        % key["path"]))
    return 0


def cmd_key_copy(args):
    key = find_key(registry_mod.load_registry(), args.name)
    if key is None:
        raise SystemExit("crow-cli: no such key %r (crow keys)" % args.name)
    print(copy_public_key(key))
    return 0


def cmd_key_rename(args):
    try:
        new = rename_key(args.name, args.new_name)
    except ValueError as error:
        raise SystemExit("crow-cli: %s" % error)
    print("%s → %s" % (args.name, new))
    return 0


def cmd_key_rm(args):
    reg = registry_mod.load_registry()
    key = next((k for k in reg.data.get("keys", [])
                if k["name"] == args.name), None)
    delete_files = False
    if key and not key.get("imported") and os.path.isfile(key["path"]):
        delete_files = args.force or input(
            "%s 와 .pub 파일도 지울까요? [y/N] " % key["path"]
        ).strip().lower() == "y"
    try:
        _key, deleted = remove_key(args.name, delete_files)
    except ValueError as error:
        raise SystemExit("crow-cli: %s" % error)
    print("%s: %s" % (args.name, "파일까지 삭제함" if deleted
                      else "등록 해제 (파일은 그대로)"))
    return 0


def install_key(record, key=None):
    """ssh-copy-id the key onto the host (interactive: may ask the
    server password once). Returns True on success."""
    args = ["ssh-copy-id"]
    if key:
        args += ["-i", key["path"]]
    if record.get("port"):
        args += ["-p", str(record["port"])]
    args.append(record["address"])
    print(" ".join(args))
    result = subprocess.run(args)
    return result.returncode == 0


def cmd_key_install(args):
    reg = registry_mod.load_registry()
    key = find_key(reg, args.key)
    if key is None:
        raise SystemExit("crow-cli: no such key %r (crow keys)" % args.key)
    record = host_record(args.host, reg)
    if not install_key(record, key):
        raise SystemExit("crow-cli: 키 설치 실패 — 주소/비밀번호를 확인하세요")
    if record.get("name") and find_host(reg, record["name"]):

        def bind(reg):
            saved = find_host(reg, record["name"])
            if saved:
                saved["identity"] = key["path"]
        with_registry(bind)
        print("%s 호스트의 키로 지정함" % record["name"])
    print("확인: crow host check %s" % (record.get("name")
                                       or record["address"]))
    return 0


def check_host(record):
    """연결 + 키 인증 확인 (Crow의 키 확인). Returns True if ssh works."""
    from .client import Client
    print("호스트: %s%s" % (record["address"],
                            ":%s" % record["port"]
                            if record.get("port") else ""))
    identity = record.get("identity")
    if identity:
        info = key_fingerprint(identity)
        print("사용 키: %s %s" % (identity,
                                  info["sha256"] if info else "(읽기 실패)"))
    else:
        print("사용 키: 기본 키 자동 탐색 (~/.ssh/id_*, ssh-agent)")
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if record.get("port"):
        args += ["-p", str(record["port"])]
    if identity:
        args += ["-o", "IdentitiesOnly=yes", "-i",
                 os.path.expanduser(identity)]
    args += [record["address"], "true"]
    result = subprocess.run(args, capture_output=True, text=True,
                            timeout=15, stdin=subprocess.DEVNULL)
    if result.returncode == 0:
        print("✓ 연결·인증 성공 — 비밀번호 없이 바로 접속됩니다")
        return True
    state = Client.classify_probe_error(result.stderr or "")
    print("✗ %s" % state)
    if "인증" in state:
        print("  → 공개키가 서버에 없으면: crow key install <키> %s"
              % (record.get("name") or record["address"]))
        print("  → 비밀번호로 접속해 키부터 넣으려면: crow ssh %s"
              % (record.get("name") or record["address"]))
    return False


def cmd_host_check(args):
    reg = registry_mod.load_registry()
    record = host_record(args.name, reg)
    return 0 if check_host(record) else 1


# -------------------------------------------------------------- ssh 접속
def ssh_command(record):
    base = " ".join(shlex.quote(a) for a in ssh_args_for(record))
    # If ssh drops instantly (bad host, auth), the pane must not die:
    # fall back to a local shell in the same pane so it can be retried.
    return base + "; echo '[crow-cli] ssh가 종료됐습니다 — 다시 붙으려면 " \
        "ssh 명령을 다시 실행하세요'; exec ${SHELL:-/bin/sh}"


def ensure_ssh_session(reg, record):
    name = "ssh-" + derive_host_name(record.get("name") or
                                      record["address"])
    registry_mod.validate_name(name)
    if reg.find(name):
        return name
    session = reg.create_session(name, kind="ssh")
    win = reg.add_window(session, name="ssh", cwd=os.getcwd(),
                         command=ssh_command(record),
                         meta={"ssh_host": record["address"],
                               "ssh_host_name": record.get("name")})
    spawn_window(win)
    return name


def cmd_ssh(args):
    reg = registry_mod.load_registry()
    record = host_record(args.host, reg)
    name = with_registry(lambda reg: ensure_ssh_session(reg, record))
    print("%s: ssh %s" % (name, record["address"]))
    if args.detached:
        return 0
    return run_attach(name)


# --------------------------------------------------------- reverse agent
def free_name(reg, base):
    """base, then base-2, base-3, … — the first unused session name."""
    name, n = base, 1
    while reg.find(name):
        n += 1
        name = "%s-%d" % (base, n)
    return name


def find_reverse_session(reg, provider, client_root, host=None,
                         local=False):
    """An existing reverse session for the same provider, folder and host
    (re-selecting it in the panel reopens it instead of launching anew)."""
    root = tools_mod.canonical(client_root)
    address = None if local else (host or {}).get("address")
    for session in reg.data["sessions"]:
        if not session["kind"].startswith("reverse-agent") or \
                not session["windows"]:
            continue
        meta = session["windows"][0].get("meta", {})
        if meta.get("provider") == provider and \
                meta.get("client_root") == root and \
                meta.get("remote_host") == address:
            return session["name"]
    return None


def launch_reverse_agent(provider, client_root, name=None, host_ref=None,
                         local=False):
    """Create (but do not attach) a reverse-agent session. Shared by the
    reverse-agent CLI command and the in-UI reverse panel."""
    client_root = tools_mod.canonical(client_root)
    if not os.path.isdir(client_root):
        raise SystemExit("crow-cli: --dir %s is not a directory"
                         % client_root)
    bridge = ensure_bridge(client_root)
    if not name:
        name = with_registry(lambda reg: free_name(reg, "reverse-" + provider))
    registry_mod.validate_name(name)
    client_id = with_registry(lambda reg: reg.data["client_id"])
    record = None

    if local:
        sandbox = os.path.join(registry_mod.home(), "sandboxes", name)
        server_home = os.path.join(sandbox, "home")
        session_root = os.path.join(sandbox, "sessions")
        os.makedirs(server_home, mode=0o700, exist_ok=True)
        request = {"provider": provider, "client_id": client_id,
                   "client_root": client_root,
                   "bridge_host": "127.0.0.1",
                   "bridge_port": bridge["port"], "token": bridge["token"],
                   "python": sys.executable, "runtime_path": ENTRY,
                   "server_home": server_home}
        prepared = reverse_mod.prepare(request)
        meta = {"client_root": client_root, "session_root": session_root,
                "server_home": server_home, "provider": provider,
                "server_directory": prepared["directory"],
                "token": bridge["token"]}
        cwd = prepared["directory"]
        command = prepared["command"]
    else:
        reg = registry_mod.load_registry()
        if host_ref:
            record = host_record(host_ref, reg)
        elif reg.data.get("hosts"):
            record = reg.data["hosts"][0]
            eprint("crow-cli: using saved host %s (%s)"
                   % (record["name"], record["address"]))
        else:
            raise SystemExit("crow-cli: reverse-agent needs --host "
                             "user@server, a saved host (crow host add), "
                             "or --local for a sandbox)")
        host = record
        eprint("crow-cli: installing runtime on %s ..." % record["address"])
        try:
            remote_install(host)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip().splitlines()
            raise SystemExit(
                "서버 접속/설치 실패 (ssh 종료 %s)%s — 주소·포트·키와 "
                "서버의 python3/cc를 확인하세요"
                % (error.returncode,
                   (": " + detail[-1][:160]) if detail else ""))
        remote_port = random.randint(49152, 65000)
        request = {"provider": provider, "client_id": client_id,
                   "client_root": client_root,
                   "bridge_host": "127.0.0.1", "bridge_port": remote_port,
                   "token": bridge["token"], "python": "python3",
                   "runtime_path": "$HOME/.crow-cli/lib/crow"}
        tunnel = subprocess.Popen(
            ssh_tunnel_args(record, ["-R", "127.0.0.1:%d:127.0.0.1:%d"
                                     % (remote_port, bridge["port"])]),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.0)
            if tunnel.poll() is not None:
                raise SystemExit("crow-cli: ssh -R tunnel failed")
            result = subprocess.run(
                ssh_base(host) + ["python3 ~/.crow-cli/lib/crow "
                                  "agent-prepare"],
                input=json.dumps(request), capture_output=True, text=True,
                timeout=60)
            if result.returncode != 0:
                raise SystemExit("crow-cli: remote prepare failed: %s"
                                 % result.stderr.strip())
            prepared = json.loads(result.stdout.strip().splitlines()[-1])
        finally:
            tunnel.terminate()
        uid = remote_exec(host, ["id", "-u"]).stdout.strip()
        meta = {"client_root": client_root, "provider": provider,
                "server_directory": prepared["directory"],
                "token": bridge["token"], "remote_host": record["address"],
                "remote_host_name": record.get("name"),
                "remote_ssh_port": record.get("port"),
                "remote_identity": record.get("identity"),
                "bridge_remote_port": remote_port,
                "remote_socket": "/tmp/crow-cli-sessions-%s/%s.sock"
                % (uid, "%s")}
        cwd = prepared["directory"]
        command = prepared["command"]

    def create(reg):
        if reg.find(name):
            reg.remove_session(name)
        session = reg.create_session(
            name, kind="reverse-agent" + ("-local" if local else ""))
        win = reg.add_window(session, name=provider, cwd=cwd,
                             command=None, meta=meta)
        pane = registry_mod.active_pane(win)
        if meta.get("remote_host"):
            win["meta"]["remote_socket"] = meta["remote_socket"] % pane["id"]
            try:
                remote_exec(remote_record(meta),
                            ["~/.crow-cli/bin/crow-session", "attach",
                             pane["id"], cwd, command], timeout=30)
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or "").strip().splitlines()
                raise SystemExit(
                    "서버에서 에이전트 세션 시작 실패%s"
                    % ((": " + detail[-1][:160]) if detail else ""))
        else:
            pane["command"] = command
            spawn_window(win)
        if record:
            saved = find_host(reg, record.get("name"))
            if saved:
                saved["last_used"] = time.time()
        print("%s:%s provider=%s server-dir=%s"
              % (name, win["name"], provider, prepared["directory"]))

    with_registry(create)
    return name


def cmd_reverse_agent(args):
    name = launch_reverse_agent(
        args.provider, args.directory or os.getcwd(),
        name=args.session, host_ref=args.host, local=args.local)
    if args.detached:
        return 0
    return run_attach(name)


# ------------------------------------------------------------------ main
def build_parser():
    parser = argparse.ArgumentParser(
        prog="crow", description="crow-cli: persistent sessions + "
        "reverse agents (tmux/herdr-like)")
    parser.add_argument("--version", action="version",
                        version="crow-cli " + __version__)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("new", help="create session (and attach)")
    p.add_argument("-s", "--session")
    p.add_argument("-n", "--window-name", dest="window_name")
    p.add_argument("-c", "--directory")
    p.add_argument("-d", "--detached", action="store_true")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("ls", help="list sessions/windows")
    p.set_defaults(fn=cmd_ls)
    sub.add_parser("list-sessions").set_defaults(fn=cmd_ls)

    p = sub.add_parser("attach", help="attach to a session")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=lambda a: run_attach(a.target))
    sub.add_parser("a").set_defaults(fn=lambda a: run_attach(None))

    p = sub.add_parser("new-window", help="new window in a session")
    p.add_argument("-t", "--target")
    p.add_argument("-n", "--window-name", dest="window_name")
    p.add_argument("-c", "--directory")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_new_window)

    p = sub.add_parser("list-windows")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=cmd_list_windows)

    p = sub.add_parser("split-window",
                       help="split the active pane (%% left/right, "
                            "\" top/bottom with prefix keys)")
    p.add_argument("-t", "--target")
    p.add_argument("--horizontal", action="store_true",
                   help="side by side (tmux -h / prefix %%)")
    p.add_argument("-v", "--vertical", action="store_true",
                   help="stacked (tmux -v / prefix \")")
    p.add_argument("-c", "--directory")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_split_window)

    p = sub.add_parser("list-panes")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=cmd_list_windows)
    p = sub.add_parser("select-pane")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(fn=cmd_select_pane)
    p = sub.add_parser("kill-pane")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(fn=lambda a: cmd_kill(a, pane=True))

    p = sub.add_parser("kill-session")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(fn=lambda a: cmd_kill(a, window=False))
    p = sub.add_parser("kill-window")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(fn=lambda a: cmd_kill(a, window=True))

    p = sub.add_parser("select-window")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("capture", help="print a window's replay buffer")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=cmd_capture)
    p = sub.add_parser("capture-pane")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("info", help="print a window's current directory")
    p.add_argument("-t", "--target")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("rename-session")
    p.add_argument("-t", "--target")
    p.add_argument("new_name")
    p.set_defaults(fn=lambda a: cmd_rename(
        argparse.Namespace(target=a.target, new_name=a.new_name,
                           window=False)))
    p = sub.add_parser("rename-window")
    p.add_argument("-t", "--target")
    p.add_argument("new_name")
    p.set_defaults(fn=lambda a: cmd_rename(
        argparse.Namespace(target=a.target, new_name=a.new_name,
                           window=True)))

    p = sub.add_parser("hosts", help="list saved reverse-agent hosts")
    p.set_defaults(fn=cmd_hosts)
    p = sub.add_parser("host", help="manage saved hosts")
    host_sub = p.add_subparsers(dest="host_cmd")
    hp = host_sub.add_parser("add")
    hp.add_argument("name", nargs="?",
                    help="저장 이름 또는 user@host (하나만 적어도 됨)")
    hp.add_argument("--address", help="user@host")
    hp.add_argument("--port", type=int)
    hp.add_argument("--identity", help="ssh identity file")
    hp.add_argument("--key", help="키 이름 (crow keys에서 선택)")
    hp.set_defaults(fn=cmd_host_add)
    hp = host_sub.add_parser("rm")
    hp.add_argument("name")
    hp.set_defaults(fn=cmd_host_rm)
    hp = host_sub.add_parser("check", help="연결 + 키 인증 확인")
    hp.add_argument("name")
    hp.set_defaults(fn=cmd_host_check)
    hp = host_sub.add_parser("list")
    hp.set_defaults(fn=cmd_hosts)
    p.set_defaults(fn=cmd_hosts)

    p = sub.add_parser("keys", help="SSH 키 목록 (관리 키 + ~/.ssh 발견)")
    p.set_defaults(fn=cmd_keys)
    p = sub.add_parser("key", help="SSH 키 관리 (Crow의 SSH Keys)")
    key_sub = p.add_subparsers(dest="key_cmd")
    kp = key_sub.add_parser("add", help="키 생성 또는 가져오기")
    kp.add_argument("name")
    kp.add_argument("--type", default="ed25519",
                    choices=["ed25519", "rsa"])
    kp.add_argument("--file", help="가져올 개인키 경로")
    kp.add_argument("--passphrase", help="새 키 암호 (기본: 물어봄)")
    kp.set_defaults(fn=cmd_key_add)
    kp = key_sub.add_parser("show", help="지문 + 공개키 표시")
    kp.add_argument("name")
    kp.set_defaults(fn=cmd_key_show)
    kp = key_sub.add_parser("copy", help="공개키를 클립보드에 복사")
    kp.add_argument("name")
    kp.set_defaults(fn=cmd_key_copy)
    kp = key_sub.add_parser("rename", help="관리 키 이름 변경")
    kp.add_argument("name")
    kp.add_argument("new_name")
    kp.set_defaults(fn=cmd_key_rename)
    kp = key_sub.add_parser("rm", help="키 삭제/등록 해제")
    kp.add_argument("name")
    kp.add_argument("--force", action="store_true")
    kp.set_defaults(fn=cmd_key_rm)
    kp = key_sub.add_parser("install", help="공개키를 서버에 설치")
    kp.add_argument("key")
    kp.add_argument("host")
    kp.set_defaults(fn=cmd_key_install)
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser("ssh", help="호스트에 SSH 접속 (세션으로 열림)")
    p.add_argument("host", help="저장된 호스트 이름 또는 user@host")
    p.add_argument("-d", "--detached", action="store_true")
    p.set_defaults(fn=cmd_ssh)

    p = sub.add_parser("reverse-agent",
                       help="run an agent CLI on a server, tools on "
                            "this client (Crow's reverse agent)")
    p.add_argument("--host", help="user@server (omit with --local)")
    p.add_argument("--local", action="store_true",
                   help="sandboxed server on this machine (tests/demo)")
    p.add_argument("--provider", default="claude",
                   choices=list(reverse_mod.PROVIDERS))
    p.add_argument("--dir", dest="directory",
                   help="client workspace (default: cwd)")
    p.add_argument("-s", "--session")
    p.add_argument("-d", "--detached", action="store_true")
    p.set_defaults(fn=cmd_reverse_agent)

    p = sub.add_parser("doctor", help="check runtime, sessions, bridges")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("build", help="build the crow-session daemon")
    p.set_defaults(fn=lambda a: (print(daemon_mod.build_binary()), 0)[1])

    # internal commands (also run on the server) -------------------------
    p = sub.add_parser("agent-tools")
    p.add_argument("--root", required=True)
    p.set_defaults(fn=lambda a: tools_mod.serve_stdio(a.root))
    p = sub.add_parser("agent-bridge")
    p.add_argument("--root", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(fn=lambda a: reverse_mod.Bridge(
        a.root, a.token, port=a.port).serve_forever())
    p = sub.add_parser("agent-proxy")
    p.add_argument("config")
    p.set_defaults(fn=lambda a: _run_proxy(a.config))
    p = sub.add_parser("agent-guard")
    p.set_defaults(fn=lambda a: (print(json.dumps(reverse_mod.guard())), 0)[1])
    p = sub.add_parser("agent-prepare")
    p.set_defaults(fn=_run_prepare)
    return parser


def _run_proxy(config_text):
    config = json.loads(config_text)
    return reverse_mod.Proxy(config.get("host", "127.0.0.1"),
                             int(config["port"]), config["token"]).run()


def _run_prepare(_args):
    request = json.load(sys.stdin)
    try:
        result = reverse_mod.prepare(request)
    except Exception as error:
        eprint(str(error))
        return 1
    print(json.dumps(result))
    return 0


def cmd_doctor(_args):
    binary = daemon_mod.binary()
    print("daemon: %s (version %s)" % (
        binary, subprocess.run([binary, "--version"], capture_output=True,
                               text=True).stdout.strip()))
    print("runtime root: %s" % daemon_mod.runtime_root())
    print("state: %s" % registry_mod.state_path())
    print("user: %s" % getpass.getuser())
    return cmd_ls(_args)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        # Bare `crow`: attach to the last session or create `main`.
        reg = registry_mod.load_registry()
        if reg.data["sessions"]:
            return run_attach(None)
        return cmd_new(argparse.Namespace(
            session="main", window_name=None, directory=None,
            detached=False, command=[]))
    try:
        result = args.fn(args)
    except RuntimeError as error:
        eprint("crow-cli: %s" % error)
        return 1
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
