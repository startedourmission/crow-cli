"""Session/window/pane registry: ~/.crow-cli/state.json (file-locked).

Model (tmux words): session > window > pane. A pane is one crow-session
daemon (one PTY); a window is a split layout of panes shown together.
Metadata survives client restarts; PTYs survive everything except an
explicit kill or a host reboot (same contract as Crow).

Layout tree (JSON):
  {"leaf": pane_id}
  {"split": "v"|"h", "ratio": 0.5, "a": <tree>, "b": <tree>}
"v" = side-by-side (vertical divider, tmux %), "h" = stacked (tmux \").

State written before panes existed (window == one PTY) is migrated on
load: the window's PTY becomes its single pane.
"""
import contextlib
import fcntl
import json
import os
import re
import time
import uuid

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def home():
    return os.environ.get("CROWCLI_HOME") or os.path.expanduser("~/.crow-cli")


def state_path():
    return os.path.join(home(), "state.json")


def validate_name(name):
    if not name or not NAME_RE.match(name) or len(name) > 64:
        raise ValueError("invalid name %r (use letters, digits, _ . -)" % (name,))
    return name


# ------------------------------------------------------------------ layout
def leaf(pane_id):
    return {"leaf": pane_id}


def layout_pane_ids(tree):
    if not tree:
        return []
    if "leaf" in tree:
        return [tree["leaf"]]
    return layout_pane_ids(tree.get("a")) + layout_pane_ids(tree.get("b"))


def split_layout(tree, target_id, new_id, direction):
    """Replace the target leaf with a split of target + new pane."""
    if "leaf" in tree:
        if tree["leaf"] == target_id:
            return {"split": direction, "ratio": 0.5,
                    "a": leaf(target_id), "b": leaf(new_id)}
        return tree
    return {"split": tree["split"], "ratio": tree.get("ratio", 0.5),
            "a": split_layout(tree["a"], target_id, new_id, direction),
            "b": split_layout(tree["b"], target_id, new_id, direction)}


def unsplit_layout(tree, pane_id):
    """Remove a leaf; its sibling subtree takes the split's place."""
    if "leaf" in tree:
        return None if tree["leaf"] == pane_id else tree
    a = unsplit_layout(tree["a"], pane_id)
    b = unsplit_layout(tree["b"], pane_id)
    if a is None:
        return b
    if b is None:
        return a
    return {"split": tree["split"], "ratio": tree.get("ratio", 0.5),
            "a": a, "b": b}


def normalize_window(window):
    """Migrate a pre-pane window (window == PTY) in place."""
    if "panes" in window and not window["panes"]:
        # A brand-new window: panes are about to be added, not legacy.
        window.setdefault("active_pane", 0)
        window.setdefault("layout", None)
        return window
    if window.get("panes"):
        window.setdefault("active_pane", 0)
        if not window.get("layout"):
            tree = None
            for pane in window["panes"]:
                tree = pane["id"] if tree is None else {
                    "split": "v", "ratio": 0.5, "a": tree,
                    "b": leaf(pane["id"])} if isinstance(tree, dict) \
                    else leaf(pane["id"])
            window["layout"] = tree if isinstance(tree, dict) else \
                leaf(window["panes"][0]["id"])
        return window
    pane = {"id": window["id"], "name": window.get("name", "pane"),
            "cwd": window.get("cwd") or os.getcwd(),
            "command": window.get("command"),
            "meta": window.get("meta") or {},
            "created_at": window.get("created_at") or time.time(),
            "exited": window.get("exited")}
    window["panes"] = [pane]
    window["active_pane"] = 0
    window["layout"] = leaf(pane["id"])
    window.setdefault("meta", {})
    return window


def window_panes(window):
    normalize_window(window)
    by_id = dict((p["id"], p) for p in window["panes"])
    ordered = [by_id[i] for i in layout_pane_ids(window["layout"])
               if i in by_id]
    for pane in window["panes"]:  # panes missing from layout go last
        if pane not in ordered:
            ordered.append(pane)
    return ordered


def active_pane(window):
    panes = window_panes(window)
    if not panes:
        return None
    idx = min(window.get("active_pane", 0), len(panes) - 1)
    return panes[idx]


def pane_by_id(window, pane_id):
    for pane in window.get("panes", []):
        if pane["id"] == pane_id:
            return pane
    return None


class Registry(object):
    def __init__(self):
        self.data = {"version": 2, "sessions": [], "client_id": None}

    # -- persistence -----------------------------------------------------
    def load(self):
        path = state_path()
        if os.path.exists(path):
            with open(path) as fh:
                self.data = json.load(fh)
        if not self.data.get("client_id"):
            self.data["client_id"] = str(uuid.uuid4())
        self.data.setdefault("hosts", [])
        self.data.setdefault("keys", [])
        for session in self.data.get("sessions", []):
            for window in session.get("windows", []):
                normalize_window(window)
        return self

    def save(self):
        os.makedirs(home(), mode=0o700, exist_ok=True)
        path = state_path()
        tmp = path + ".tmp.%d" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    @contextlib.contextmanager
    def locked(self):
        os.makedirs(home(), mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(home(), "state.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.load()
            yield self
            self.save()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- sessions ----------------------------------------------------------
    def find(self, name=None):
        sessions = self.data["sessions"]
        if name:
            for s in sessions:
                if s["name"] == name:
                    return s
            return None
        if not sessions:
            return None
        # Most recently used, like tmux: last attach (switches included)
        # or creation, whichever is newer; ties go to the later entry.
        return max(reversed(sessions), key=lambda s: max(
            s.get("last_attached") or 0, s.get("created_at") or 0))

    def create_session(self, name, kind="local", meta=None):
        validate_name(name)
        if self.find(name):
            raise ValueError("session %r already exists" % name)
        session = {"name": name, "kind": kind, "meta": meta or {},
                   "windows": [], "active": 0, "created_at": time.time(),
                   "last_attached": 0}
        self.data["sessions"].append(session)
        return session

    def remove_session(self, name):
        self.data["sessions"] = [s for s in self.data["sessions"]
                                 if s["name"] != name]

    # -- windows / panes ---------------------------------------------------
    def add_window(self, session, name=None, cwd=None, command=None,
                   meta=None):
        index = len(session["windows"])
        window = {"id": str(uuid.uuid4()),
                  "name": name or "win%d" % index,
                  "meta": meta or {}, "created_at": time.time(),
                  "panes": [], "active_pane": 0, "layout": None}
        session["windows"].append(window)
        session["active"] = len(session["windows"]) - 1
        self.add_pane(window, cwd=cwd, command=command, meta=meta)
        return window

    def add_pane(self, window, name=None, cwd=None, command=None, meta=None,
                 direction=None, target_id=None):
        normalize_window(window)
        pane = {"id": str(uuid.uuid4()),
                "name": name or "pane%d" % len(window["panes"]),
                "cwd": cwd or os.getcwd(), "command": command,
                "meta": meta or {}, "created_at": time.time(),
                "exited": None}
        window["panes"].append(pane)
        if direction and target_id:
            window["layout"] = split_layout(window["layout"], target_id,
                                            pane["id"], direction)
        elif not window.get("layout"):
            window["layout"] = leaf(pane["id"])
        window["active_pane"] = window_panes(window).index(pane)
        return pane

    def remove_pane(self, window, pane_id):
        normalize_window(window)
        window["panes"] = [p for p in window["panes"]
                           if p["id"] != pane_id]
        window["layout"] = unsplit_layout(window["layout"], pane_id)
        if window.get("zoomed") == pane_id:
            window["zoomed"] = None
        if window["active_pane"] >= len(window["panes"]):
            window["active_pane"] = max(0, len(window["panes"]) - 1)

    def remove_window(self, session, window_id):
        session["windows"] = [w for w in session["windows"]
                              if w["id"] != window_id]
        if session["active"] >= len(session["windows"]):
            session["active"] = max(0, len(session["windows"]) - 1)

    @staticmethod
    def active_window(session):
        if not session or not session["windows"]:
            return None
        idx = min(session.get("active", 0), len(session["windows"]) - 1)
        return session["windows"][idx]


def load_registry():
    return Registry().load()
