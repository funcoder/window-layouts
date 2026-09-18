#!/usr/bin/env python3
"""Window layout templates for Hyprland on Omarchy.

    layouts.py list
    layouts.py save NAME [--workspaces 1,2]
    layouts.py apply NAME [--keep-others] [--timeout SECONDS] [--workspaces 1,2] [--notify]
    layouts.py delete NAME
    layouts.py autosave
    layouts.py startup NAME [apply options]

`list` prints a single JSON document. Every other command prints one JSON
event per line ({"event": "progress" | "saved" | "done" | "deleted" |
"error", ...}) so the bar panel can render progress while a restore runs.

Templates live in ~/.config/omarchy/window-layouts/<slug>.json. They are
plain JSON: each window records its class, the command used to relaunch it,
its workspace/monitor and geometry, so a template can be hand-edited (for
example to change a launch command). A terminal's command includes whatever
was running in it, so a window sitting at a prompt and one running `claude`
are both saved, and told apart from each other when the template is applied.

Restoring a template:
  1. match template windows to windows that are already open (same app and
     launch command first, then same class),
  2. close every other window (unless --keep-others),
  3. launch whatever is missing and wait for the new windows to map,
  4. park all matched windows on a hidden special workspace, then rebuild
     each workspace: the saved tiled rectangles are turned back into a
     dwindle split tree and windows are re-inserted with `preselect` and an
     exact `splitratio`, so the tiling comes back as it was saved,
  5. float/size/position floating windows, re-apply fullscreen and pins,
     and focus the workspaces that were active when the template was saved.
"""

import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "omarchy", "window-layouts")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "omarchy", "window-layouts")
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "omarchy-window-layouts")

AUTOSAVE_NAME = "Last session"
PREVIOUS_NAME = "Previous session"
AUTOSAVE_GRACE_SECONDS = 600
STAGING = "special:wl-restore"
EDGE_TOLERANCE = 30

BROWSERS = {
    "chromium", "chrome", "google-chrome", "google-chrome-stable", "google-chrome-beta",
    "brave", "brave-browser", "vivaldi", "vivaldi-stable", "microsoft-edge",
    "microsoft-edge-stable", "msedge", "helium", "thorium",
}
TERMINALS = {"foot", "alacritty", "kitty", "ghostty"}
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu", "elvish", "xonsh"}
# How each terminal takes a command to run: foot and kitty read it as trailing
# arguments, the others want it after -e.
TERMINAL_COMMAND_FLAG = {"foot": [], "kitty": [], "alacritty": ["-e"], "ghostty": ["-e"]}
# Only remember a command that has been running for a while, so a snapshot
# doesn't immortalise whatever was typed a second before it was taken.
COMMAND_MIN_AGE = 20
# Set from --no-commands: record terminals as a bare shell instead.
CAPTURE_COMMANDS = True
# A command line can carry a password or token as an argument. Templates are
# ordinary files that live on for months, so a command that looks like it
# carries a credential is never written down at all.
SECRET_FLAG = re.compile(
    r"^--?[A-Za-z0-9_\-]*(?:password|passwd|token|api[-_]?key|apikey|secret|auth|bearer|credential|"
    r"access[-_]?key|private[-_]?key|session[-_]?key|passphrase|pat)[A-Za-z0-9_\-]*$", re.I)
SECRET_ASSIGN = re.compile(
    r"^--?[A-Za-z0-9_\-]*(?:password|passwd|token|api[-_]?key|apikey|secret|auth|bearer|credential|"
    r"access[-_]?key|private[-_]?key|session[-_]?key|passphrase)[A-Za-z0-9_\-]*=", re.I)
SECRET_IN_URL = re.compile(
    r"[?&#](?:password|passwd|token|access[-_]?token|id[-_]?token|api[-_]?key|apikey|secret|auth|"
    r"code|key|sig|signature)=[^&\s]", re.I)
SECRET_SHAPED = re.compile(
    r"(?:sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprse]-[A-Za-z0-9\-]{10,}|"
    r"AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.|://[^/\s:@]+:[^/\s@]+@)")

# Ceilings on anything read back off disk, so a tampered or corrupt template
# can't drive the restore into a huge or endless job.
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024
MAX_WINDOWS = 120
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
MAX_FIELD_CHARS = 1024
WEBAPP_CLASS = re.compile(r"^(chrome|brave|msedge|vivaldi|helium)-(.+)-([^-]+)$")


# --------------------------------------------------------------------------- output

def emit(event, **fields):
    fields["event"] = event
    try:
        sys.stdout.write(json.dumps(fields) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # The terminal that started us may have been closed by the restore.
        sys.stdout = open(os.devnull, "w")


def log(message):
    try:
        sys.stderr.write("window-layouts: " + message + "\n")
    except (BrokenPipeError, OSError):
        pass


def notify(title, body=""):
    if not shutil.which("notify-send"):
        return
    subprocess.run(["notify-send", "-a", "Window Layouts", title, body],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# --------------------------------------------------------------------------- hyprland

def hyprctl_json(what):
    try:
        out = subprocess.run(["hyprctl", "-j", what], capture_output=True, text=True, timeout=5)
        return json.loads(out.stdout or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def live_clients():
    clients = hyprctl_json("clients") or []
    return [c for c in clients if c.get("address") and c.get("mapped", True)]


def lua_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(int(value)) if float(value).is_integer() else repr(value)
    s = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return '"' + s + '"'


def dispatch(expr):
    try:
        out = subprocess.run(["hyprctl", "dispatch", expr], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"dispatch failed: {expr}: {e}")
        return False
    result = (out.stdout or "").strip()
    if result != "ok":
        log(f"dispatch: {expr} -> {result}")
    return result == "ok"


def dsp(fn, **args):
    body = ", ".join(f"{k} = {lua_value(v)}" for k, v in args.items())
    return dispatch(f"hl.dsp.{fn}({{ {body} }})")


def layout_msg(message):
    return dispatch(f"hl.dsp.layout({lua_value(message)})")


def window_sel(address):
    return "address:" + address


def workspace_selector(ws_id, ws_name):
    name = str(ws_name or "")
    if name.startswith("special:"):
        return name
    if name == "" or name == str(ws_id):
        return str(ws_id)
    return "name:" + name


def is_special(selector):
    return str(selector).startswith("special:")


def workspace_keys(ws_id, ws_name):
    name = str(ws_name or "")
    return {str(ws_id), name, name.replace("special:", "")}


def parse_workspace_filter(value):
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


# --------------------------------------------------------------------------- process introspection

def read_argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    # Chromium-based apps rewrite argv into one space-joined string.
    if len(argv) == 1 and " " in argv[0] and not os.path.exists(argv[0]):
        try:
            argv = shlex.split(argv[0])
        except ValueError:
            argv = argv[0].split()
    return argv


def read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def read_environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def proc_stat(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        fields = raw.rsplit(")", 1)[1].split()
        return {"pgrp": int(fields[2]), "tpgid": int(fields[5]), "starttime": int(fields[19])}
    except (IndexError, ValueError):
        return None


_boot_time = None


def boot_time():
    global _boot_time
    if _boot_time is None:
        _boot_time = 0.0
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        _boot_time = float(line.split()[1])
                        break
        except (OSError, ValueError):
            pass
    return _boot_time


def process_age(pid):
    info = proc_stat(pid)
    if not info or not boot_time():
        return 0.0
    started = boot_time() + info["starttime"] / os.sysconf("SC_CLK_TCK")
    return max(0.0, time.time() - started)


def child_pids(pid):
    kids = []
    try:
        for task in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{task}/children") as f:
                kids.extend(int(k) for k in f.read().split())
    except (OSError, ValueError):
        pass
    return kids


def ancestor_pids():
    pids = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def flatpak_app(pid):
    try:
        with open(f"/proc/{pid}/root/.flatpak-info") as f:
            for line in f:
                if line.startswith("name="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def resolve_exe(pid, arg0):
    if os.path.isabs(arg0) and os.path.exists(arg0):
        return arg0
    if shutil.which(arg0):
        return arg0
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return arg0


def path_command(exe):
    base = os.path.basename(exe)
    return base if shutil.which(base) else exe


# --------------------------------------------------------------------------- desktop entries

def application_dirs():
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    data_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs = [os.path.join(data_home, "applications")]
    dirs += [os.path.join(d, "applications") for d in data_dirs if d]
    dirs += [os.path.join(HOME, ".local/share/flatpak/exports/share/applications"),
             "/var/lib/flatpak/exports/share/applications"]
    seen, result = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            result.append(d)
    return result


_desktop_cache = None


def desktop_entries():
    global _desktop_cache
    if _desktop_cache is not None:
        return _desktop_cache
    entries = []
    for base in application_dirs():
        for root, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(".desktop"):
                    continue
                path = os.path.join(root, name)
                desktop_id = os.path.relpath(path, base).replace("/", "-")
                fields = {}
                try:
                    with open(path, errors="replace") as f:
                        in_entry = False
                        for line in f:
                            line = line.strip()
                            if line.startswith("["):
                                in_entry = line == "[Desktop Entry]"
                                continue
                            if in_entry and "=" in line:
                                k, v = line.split("=", 1)
                                fields.setdefault(k.strip(), v.strip())
                except OSError:
                    continue
                entries.append((desktop_id, fields))
    _desktop_cache = entries
    return entries


def desktop_for_class(cls):
    if not cls:
        return None
    lower = cls.lower()
    for desktop_id, fields in desktop_entries():
        if fields.get("StartupWMClass", "").lower() == lower:
            return desktop_id
    for desktop_id, _fields in desktop_entries():
        if desktop_id[:-len(".desktop")].lower() == lower:
            return desktop_id
    return None


def webapp_class_key(url):
    m = re.match(r"^[a-z]+://([^/?#]+)([^?#]*)", url.strip().strip('"'))
    if not m:
        return None
    host, path = m.group(1), m.group(2) or "/"
    return host + "_" + path.replace("/", "_")


def webapp_url_for_key(key):
    for _desktop_id, fields in desktop_entries():
        exec_line = fields.get("Exec", "")
        if "--app=" not in exec_line and "omarchy-launch-webapp" not in exec_line:
            continue
        try:
            parts = shlex.split(exec_line)
        except ValueError:
            continue
        for i, part in enumerate(parts):
            url = None
            if part.startswith("--app="):
                url = part.split("=", 1)[1]
            elif part.endswith("omarchy-launch-webapp") and i + 1 < len(parts):
                url = parts[i + 1]
            if url and webapp_class_key(url) == key:
                return url
    return None


# --------------------------------------------------------------------------- describing windows

def browser_launch(argv, cls):
    """Work out how to reopen one browser window.

    A single browser process usually owns several windows, so its argv only
    describes whichever window happened to start it: reading it for every
    window makes three tabs of the same web app out of three different ones.
    The class is per-window, so it decides, and argv is trusted only where it
    agrees with the class.
    """
    cmd = path_command(argv[0]) if argv else "chromium"
    keep = [a for a in argv if a.startswith(("--user-data-dir=", "--class=", "--profile-directory="))]
    has_webapp_launcher = shutil.which("omarchy-launch-webapp") is not None
    app = next((a.split("=", 1)[1] for a in argv if a.startswith("--app=")), None)

    def webapp(url, extra):
        return ["omarchy-launch-webapp", url, *extra] if has_webapp_launcher else [cmd, "--app=" + url, *extra]

    if cls.startswith("crx_"):
        return [cmd, "--profile-directory=Default", "--app-id=" + cls[4:]]

    # Web app windows carry the URL in their class:
    # chrome-<host>_<path with / as _>-<profile>.
    m = WEBAPP_CLASS.match(cls)
    if m and "_" in m.group(2):
        key, profile = m.group(2), m.group(3)
        if app and webapp_class_key(app) == key:
            return webapp(app, keep)  # this process really is this window's
        url = webapp_url_for_key(key)
        if not url:
            host, _, path = key.partition("_")
            url = "https://" + host + path.replace("_", "/")
        extra = [] if profile == "Default" else ["--profile-directory=" + profile]
        return webapp(url, extra)

    # A plain browser window. Its process may also be hosting web apps, so
    # their --app=/--class= flags must not leak into this window's command.
    if cls.lower() in BROWSERS:
        return [cmd, "--new-window"] if app else [cmd, *keep, "--new-window"]

    if app:
        return webapp(app, keep)
    app_id = next((a for a in argv if a.startswith("--app-id=")), None)
    if app_id:
        return [cmd, *keep, app_id]
    return [cmd, "--new-window"]


def strip_flag(argv, long_flag, short_flag=None):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == long_flag or (short_flag and a == short_flag):
            skip = True
            continue
        if a.startswith(long_flag + "="):
            continue
        out.append(a)
    return out


def secret_bearing(argv):
    """True when a command line looks like it carries a credential."""
    previous = ""
    for raw in argv:
        arg = str(raw)
        if SECRET_ASSIGN.match(arg) or SECRET_IN_URL.search(arg) or SECRET_SHAPED.search(arg):
            return True
        # "--token abc123": the flag is on the previous argument.
        if SECRET_FLAG.match(previous) and not arg.startswith("-"):
            return True
        previous = arg
    return False


def recordable(argv):
    """A command is only written to a template when it holds no credentials."""
    return bool(argv) and not secret_bearing(argv)


def terminal_command(shell_pid):
    """The command running in a terminal, or (None, None) for a bare prompt.

    A shell's tpgid names the process group that currently owns the terminal,
    so the window running `claude` and the one sitting at a prompt are told
    apart — and come back the way they were.
    """
    info = proc_stat(shell_pid)
    if not info or info["tpgid"] <= 0 or info["tpgid"] == info["pgrp"]:
        return None, None  # nothing running: the shell itself is in the foreground
    ours = ancestor_pids()
    script = os.path.basename(__file__)
    for kid in child_pids(shell_pid):
        kid_info = proc_stat(kid)
        if not kid_info or kid_info["pgrp"] != info["tpgid"]:
            continue
        argv = read_argv(kid)
        if not argv or os.path.basename(argv[0]).lstrip("-") in SHELLS:
            continue
        # Don't record this helper itself when it was started from a terminal —
        # but do record whatever that terminal was already running it under.
        if kid in ours and any(os.path.basename(a) == script for a in argv):
            continue
        if process_age(kid) < COMMAND_MIN_AGE:
            continue
        if not recordable(argv):
            # Restores at a bare prompt rather than writing the secret down.
            return None, None
        return argv, read_cwd(kid)
    return None, None


def terminal_launch(pid, argv, cwd):
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == "foot" and "--server" in rest:
        rest = []

    shell_pid, shell_name, other_kid = None, "bash", None
    for kid in child_pids(pid):
        kid_argv = read_argv(kid)
        if not kid_argv:
            continue
        base = os.path.basename(kid_argv[0]).lstrip("-")
        if base in SHELLS:
            shell_pid, shell_name = kid, base
            break
        # A terminal started with its own command (foot -e cliamp) has no shell
        # of ours; argv already carries the command, so it needs nothing added.
        other_kid = other_kid or kid

    command, command_cwd = (None, None)
    if shell_pid is not None and CAPTURE_COMMANDS:
        command, command_cwd = terminal_command(shell_pid)

    shell_cwd = read_cwd(shell_pid) if shell_pid is not None else (
        read_cwd(other_kid) if other_kid is not None else None)
    workdir = command_cwd or shell_cwd or cwd

    if name in ("foot", "alacritty", "ghostty"):
        rest = strip_flag(rest, "--working-directory", "-D" if name == "foot" else None)
        flags = ["--working-directory=" + workdir] if workdir and name != "alacritty" else (
            ["--working-directory", workdir] if workdir else [])
    elif name == "kitty":
        rest = strip_flag(rest, "--directory", "-d")
        flags = ["--directory", workdir] if workdir else []
    else:
        flags = []

    launch = [path_command(argv[0]), *flags, *rest]
    if command:
        # Run it through the shell and hand the window back afterwards, so the
        # terminal stays open where the command stops.
        launch += [*TERMINAL_COMMAND_FLAG.get(name, ["-e"]), shell_name, "-c",
                   shlex.join(command) + "; exec " + shell_name]
    return launch, workdir


def describe(client):
    pid = client.get("pid") or 0
    cls = client.get("class") or ""
    argv = read_argv(pid) if pid > 0 else []
    cwd = read_cwd(pid) if pid > 0 else ""
    exe_name = os.path.basename(argv[0]) if argv else ""
    launch = None

    appimage = read_environ(pid).get("APPIMAGE") if pid > 0 else None
    flatpak = flatpak_app(pid) if pid > 0 else None

    if flatpak:
        launch = ["flatpak", "run", flatpak]
    elif appimage:
        launch = [appimage]
    elif exe_name in BROWSERS or cls.startswith("crx_") or (WEBAPP_CLASS.match(cls) and "__" in cls):
        launch = browser_launch(argv, cls)
    elif exe_name in TERMINALS:
        launch, cwd = terminal_launch(pid, argv, cwd)
    elif argv:
        launch = [resolve_exe(pid, argv[0]), *argv[1:]]

    if launch and not recordable(launch):
        launch = None  # fall back to the desktop entry rather than store a secret
    if not launch:
        desktop_id = desktop_for_class(cls)
        if desktop_id:
            launch = [desktop_id]

    return {
        "class": cls,
        "initialClass": client.get("initialClass") or "",
        "launch": launch or [],
        "cwd": cwd,
    }


def signature(entry):
    return entry.get("class", "") + "\x1f" + json.dumps(entry.get("launch") or [])


# --------------------------------------------------------------------------- storage

def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "template"


def template_path(name):
    return os.path.join(CONFIG_DIR, slugify(name) + ".json")


def private_dir(path, create=True):
    """A directory descriptor for `path`, proven to be our own private directory.

    Templates record command lines and working directories, so the files are
    kept 0600 inside a 0700 directory and every open is descriptor-relative and
    refuses to follow symlinks.
    """
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"{path} is not owned by this user")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o700)
    except Exception:
        os.close(fd)
        raise
    return fd


def write_json(path, data):
    directory, name = os.path.split(path)
    dir_fd = private_dir(directory)
    tmp = name + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def read_json(path):
    directory, name = os.path.split(path)
    try:
        dir_fd = private_dir(directory, create=False)
    except OSError:
        return None
    try:
        # O_NONBLOCK so a FIFO left in place of the file can't stall the restore.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        os.close(dir_fd)
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_TEMPLATE_BYTES:
            return None
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(MAX_TEMPLATE_BYTES + 1)
        if len(raw) > MAX_TEMPLATE_BYTES:
            log(f"{path}: larger than {MAX_TEMPLATE_BYTES} bytes, ignored")
            return None
        return json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


# ------------------------------------------------------------------- validation

def clip(value, limit=MAX_FIELD_CHARS):
    return value[:limit] if isinstance(value, str) else ""


def pair(value):
    if not isinstance(value, list) or len(value) != 2:
        return None
    out = []
    for n in value:
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            return None
        out.append(int(n))
    return out


def clean_window(entry):
    """One template window, or None when it isn't shaped like one."""
    if not isinstance(entry, dict):
        return None
    argv = entry.get("launch")
    if not isinstance(argv, list) or len(argv) > MAX_ARGV:
        return None
    launch = []
    for arg in argv:
        if not isinstance(arg, str) or len(arg) > MAX_ARG_CHARS or "\x00" in arg:
            return None
        launch.append(arg)
    # A template edited by hand (or by something else) doesn't get to smuggle a
    # credential back in, and never gets to run a shell one-liner we didn't write.
    if launch and not recordable(launch):
        return None
    out = dict(entry)
    out["launch"] = launch
    out["class"] = clip(entry.get("class"))
    out["initialClass"] = clip(entry.get("initialClass"))
    out["cwd"] = clip(entry.get("cwd"))
    out["workspace"] = clip(entry.get("workspace"), 128)
    for field in ("at", "size"):
        if field in out:
            value = pair(out.get(field))
            if value is None:
                return None
            out[field] = value
    return out


def clean_template(data, path=""):
    """Validate a template read off disk before any of it is acted on."""
    if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
        return None
    windows = data["windows"]
    if len(windows) > MAX_WINDOWS:
        log(f"{path or 'template'}: more than {MAX_WINDOWS} windows, truncated")
        windows = windows[:MAX_WINDOWS]
    cleaned = [w for w in (clean_window(w) for w in windows) if w]
    out = dict(data)
    out["windows"] = cleaned
    out["name"] = clip(data.get("name"), 128)
    return out


def all_templates():
    if not os.path.isdir(CONFIG_DIR):
        return []
    result = []
    for name in sorted(os.listdir(CONFIG_DIR)):
        if name.endswith(".json"):
            full = os.path.join(CONFIG_DIR, name)
            data = clean_template(read_json(full), full)
            if data is not None:
                if not data.get("name"):
                    data["name"] = name[:-5]
                data["_path"] = full
                result.append(data)
    return result


def load_template(name):
    data = clean_template(read_json(template_path(name)), template_path(name))
    if data is not None and str(data.get("name", "")).lower() == name.lower():
        data["_path"] = template_path(name)
        return data
    for tpl in all_templates():
        if str(tpl.get("name", "")).lower() == name.lower():
            return tpl
    return None


def load_state():
    return read_json(os.path.join(STATE_DIR, "state.json")) or {}


def save_state(**changes):
    state = load_state()
    state.update(changes)
    write_json(os.path.join(STATE_DIR, "state.json"), state)


def session_info():
    """Per-login marker in $XDG_RUNTIME_DIR, which is emptied on logout/reboot."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    path = os.path.join(RUNTIME_DIR, "session.json")
    info = read_json(path)
    if not isinstance(info, dict) or "id" not in info:
        info = {"id": uuid.uuid4().hex, "started": time.time()}
        write_json(path, info)
    return info


class RestoreLock:
    def __init__(self):
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        self.fd = open(os.path.join(RUNTIME_DIR, "restore.lock"), "w")

    def acquire(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass


# --------------------------------------------------------------------------- snapshot

def snapshot(ws_filter=None):
    clients = live_clients()
    monitors = hyprctl_json("monitors") or []
    mon_by_id = {m.get("id"): m for m in monitors}

    windows = []
    for c in clients:
        ws = c.get("workspace") or {}
        if ws.get("name") == STAGING:
            continue
        if ws_filter and not (workspace_keys(ws.get("id"), ws.get("name")) & ws_filter):
            continue
        mon = mon_by_id.get(c.get("monitor"), {})
        entry = describe(c)
        entry.update({
            "title": c.get("title") or "",
            "workspace": workspace_selector(ws.get("id"), ws.get("name")),
            "workspaceId": ws.get("id"),
            "monitor": mon.get("name", ""),
            "monitorOrigin": [mon.get("x", 0), mon.get("y", 0)],
            "floating": bool(c.get("floating")),
            "at": list(c.get("at") or [0, 0]),
            "size": list(c.get("size") or [0, 0]),
            "fullscreen": int(c.get("fullscreen") or 0),
            "pinned": bool(c.get("pinned")),
            "hidden": bool(c.get("hidden")),
        })
        windows.append(entry)

    windows.sort(key=lambda w: (is_special(w["workspace"]), w.get("workspaceId") or 0,
                                w["floating"], w["at"][1], w["at"][0]))

    return {
        "version": 1,
        "savedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "monitors": [{
            "name": m.get("name", ""),
            "x": m.get("x", 0),
            "y": m.get("y", 0),
            "width": m.get("width", 0),
            "height": m.get("height", 0),
            "focused": bool(m.get("focused")),
            "activeWorkspace": workspace_selector((m.get("activeWorkspace") or {}).get("id"),
                                                 (m.get("activeWorkspace") or {}).get("name")),
        } for m in monitors],
        "windows": windows,
    }


def workspace_labels(windows):
    labels = []
    for w in windows:
        sel = str(w.get("workspace", ""))
        label = sel.replace("special:", "*").replace("name:", "")
        if label not in labels:
            labels.append(label)
    return labels


# --------------------------------------------------------------------------- restore

def build_tree(items):
    """Turn tiled rectangles back into a binary split tree (guillotine cuts)."""
    if len(items) == 1:
        return {"leaf": items[0]}

    xs0 = min(it["rect"][0] for it in items)
    ys0 = min(it["rect"][1] for it in items)
    xs1 = max(it["rect"][0] + it["rect"][2] for it in items)
    ys1 = max(it["rect"][1] + it["rect"][3] for it in items)
    box = (xs0, ys0, xs1 - xs0, ys1 - ys0)

    def find_cut(axis):
        i0, i1 = (0, 2) if axis == "x" else (1, 3)
        for edge in sorted({it["rect"][i0] + it["rect"][i1] for it in items}):
            first = [it for it in items if it["rect"][i0] + it["rect"][i1] <= edge + EDGE_TOLERANCE]
            second = [it for it in items if it["rect"][i0] >= edge - EDGE_TOLERANCE and it not in first]
            if first and second and len(first) + len(second) == len(items):
                next_start = min(it["rect"][i0] for it in second)
                return first, second, (edge + next_start) / 2
        return None

    preferred = "x" if box[2] > box[3] else "y"
    for axis in (preferred, "y" if preferred == "x" else "x"):
        cut = find_cut(axis)
        if cut:
            first, second, position = cut
            start, size = (box[0], box[2]) if axis == "x" else (box[1], box[3])
            ratio = 2 * (position - start) / size if size > 0 else 1.0
            return {
                "axis": axis,
                "ratio": max(0.1, min(1.9, ratio)),
                "first": build_tree(first),
                "second": build_tree(second),
            }

    # Not a guillotine layout (should not happen with dwindle): chain the rest.
    return {"axis": preferred, "ratio": 1.0, "first": {"leaf": items[0]}, "second": build_tree(items[1:])}


def first_leaf(node):
    while "leaf" not in node:
        node = node["first"]
    return node["leaf"]


def focus_window(address):
    dsp("focus", window=window_sel(address))
    active = hyprctl_json("activewindow") or {}
    if active.get("address") == address:
        at, size = active.get("at") or [0, 0], active.get("size") or [0, 0]
        # Dwindle splits the focused window, or the one under the cursor when
        # use_active_for_splits is off; keep both pointing at the same window.
        dsp("cursor.move", x=int(at[0] + size[0] / 2), y=int(at[1] + size[1] / 2))


def realize(node, workspace):
    if "leaf" in node:
        return
    anchor = first_leaf(node["first"])["address"]
    incoming = first_leaf(node["second"])["address"]
    focus_window(anchor)
    layout_msg("preselect " + ("r" if node["axis"] == "x" else "d"))
    dsp("window.move", window=window_sel(incoming), workspace=workspace, follow=False)
    focus_window(anchor)
    layout_msg(f"splitratio {node['ratio']:.4f} exact")
    realize(node["first"], workspace)
    realize(node["second"], workspace)


def ensure_workspace_monitor(workspace, monitor_name, monitors_by_name):
    if not monitor_name or monitor_name not in monitors_by_name or is_special(workspace):
        return
    for ws in hyprctl_json("workspaces") or []:
        if workspace_selector(ws.get("id"), ws.get("name")) == workspace:
            if ws.get("monitor") != monitor_name:
                dsp("workspace.move", workspace=workspace, monitor=monitor_name)
            return


def arrange(template, wanted, claimed, focus_after):
    live = {c["address"]: c for c in live_clients()}
    claimed = {i: a for i, a in claimed.items() if a in live}
    monitors_by_name = {m.get("name"): m for m in (hyprctl_json("monitors") or [])}

    # 1. Park every template window on a hidden special workspace with the
    #    right floating state, so each workspace can be rebuilt from empty.
    for i, address in claimed.items():
        client, entry = live[address], wanted[i]
        if client.get("fullscreen"):
            dsp("window.fullscreen", window=window_sel(address), action="unset")
        if client.get("pinned"):
            dsp("window.pin", window=window_sel(address), action="disable")
        dsp("window.move", window=window_sel(address), workspace=STAGING, follow=False)
        if bool(client.get("floating")) != entry["floating"]:
            dsp("window.float", window=window_sel(address), action="enable" if entry["floating"] else "disable")

    # 2. Rebuild workspace by workspace, in template order.
    by_workspace = {}
    for i in sorted(claimed):
        by_workspace.setdefault(wanted[i]["workspace"], []).append(i)

    done = 0
    total = len(claimed)
    for workspace, indexes in by_workspace.items():
        monitor_name = wanted[indexes[0]].get("monitor", "")
        label = workspace.replace("special:", "special ").replace("name:", "")
        emit("progress", stage="arrange", placed=done, total=total, message=f"Arranging workspace {label}")

        tiled = [i for i in indexes if not wanted[i]["floating"] and not wanted[i].get("hidden") and not wanted[i].get("fullscreen")]
        extra = [i for i in indexes if not wanted[i]["floating"] and i not in tiled]
        floats = [i for i in indexes if wanted[i]["floating"]]

        if tiled and not is_special(workspace):
            items = [{"address": claimed[i], "rect": (*wanted[i]["at"], *wanted[i]["size"])} for i in tiled]
            tree = build_tree(items)
            dsp("window.move", window=window_sel(first_leaf(tree)["address"]), workspace=workspace, follow=False)
            ensure_workspace_monitor(workspace, monitor_name, monitors_by_name)
            realize(tree, workspace)
        else:
            extra = tiled + extra

        for i in extra:
            dsp("window.move", window=window_sel(claimed[i]), workspace=workspace, follow=False)
        if extra:
            ensure_workspace_monitor(workspace, monitor_name, monitors_by_name)

        for i in floats:
            entry, address = wanted[i], claimed[i]
            dsp("window.move", window=window_sel(address), workspace=workspace, follow=False)
            ensure_workspace_monitor(workspace, monitor_name, monitors_by_name)
            w, h = entry["size"]
            x, y = entry["at"]
            current = monitors_by_name.get(monitor_name)
            if current:
                ox, oy = entry.get("monitorOrigin") or [0, 0]
                x, y = x - ox + current.get("x", 0), y - oy + current.get("y", 0)
            dsp("window.resize", window=window_sel(address), x=w, y=h)
            dsp("window.move", window=window_sel(address), x=x, y=y)

        for i in indexes:
            entry, address = wanted[i], claimed[i]
            if entry.get("pinned") and entry["floating"]:
                dsp("window.pin", window=window_sel(address), action="enable")
            if entry.get("fullscreen"):
                mode = "maximized" if entry["fullscreen"] == 1 else "fullscreen"
                dsp("window.fullscreen", window=window_sel(address), action="set", mode=mode)

        done += len(indexes)

    # 3. Anything still parked (a move failed) goes back to its workspace.
    for c in live_clients():
        if (c.get("workspace") or {}).get("name") == STAGING:
            target = next((wanted[i]["workspace"] for i, a in claimed.items() if a == c["address"]), None)
            if target:
                dsp("window.move", window=window_sel(c["address"]), workspace=target, follow=False)

    # 4. Focus what was visible when the template was saved.
    for workspace in focus_after:
        if workspace:
            dsp("focus", workspace=workspace)


def launch(entry):
    argv = list(entry.get("launch") or [])
    if not argv:
        return False
    cwd = entry.get("cwd") if entry.get("cwd") and os.path.isdir(entry.get("cwd")) else HOME

    if argv[0].endswith(".desktop"):
        cmd = ["uwsm-app", "--", argv[0]] if shutil.which("uwsm-app") else ["gtk-launch", argv[0][:-8]]
    elif os.path.basename(argv[0]).startswith("omarchy-launch") or argv[0] in ("uwsm-app", "uwsm", "setsid"):
        cmd = argv
    elif shutil.which("uwsm-app"):
        cmd = ["uwsm-app", "--", *argv]
    else:
        cmd = argv

    try:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as e:
        log(f"launch failed: {cmd}: {e}")
        return False


def geometry_distance(entry, client):
    ax, ay = (entry.get("at") or [0, 0])[:2]
    aw, ah = (entry.get("size") or [0, 0])[:2]
    bx, by = (client.get("at") or [0, 0])[:2]
    bw, bh = (client.get("size") or [0, 0])[:2]
    return abs(ax - bx) + abs(ay - by) + abs(aw - bw) + abs(ah - bh)


def match_windows(wanted, clients, described):
    claimed, used = {}, set()
    passes = (
        lambda e, c, d: signature(e) == signature(d) and e["workspace"] == workspace_selector(c["workspace"]["id"], c["workspace"]["name"]),
        lambda e, c, d: signature(e) == signature(d),
        lambda e, c, d: e["class"] != "" and e["class"] == d["class"],
    )
    for same in passes:
        for i, entry in enumerate(wanted):
            if i in claimed:
                continue
            # Several windows can be equally good matches (two terminals in the
            # same directory). Take whichever is already closest to where this
            # entry belongs, so windows stay where they are instead of swapping.
            best = min((c for c in clients
                        if c["address"] not in used and same(entry, c, described[c["address"]])),
                       key=lambda c: geometry_distance(entry, c), default=None)
            if best is not None:
                claimed[i] = best["address"]
                used.add(best["address"])
    return claimed, used


def apply(name, keep_others=False, timeout=45, ws_filter=None, show_notification=False):
    template = load_template(name)
    if not template:
        emit("error", message=f"No template named “{name}”")
        return 1

    lock = RestoreLock()
    if not lock.acquire():
        emit("error", message="Another restore is already running")
        return 1

    # Closing windows can close the terminal that started us; keep going.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        wanted = [w for w in template["windows"]
                  if not ws_filter or workspace_keys(w.get("workspaceId"), str(w.get("workspace", "")).replace("name:", "")) & ws_filter]
        total = len(wanted)
        name = template.get("name", name)

        clients = [c for c in live_clients() if (c.get("workspace") or {}).get("name") != STAGING]
        described = {c["address"]: describe(c) for c in clients}
        claimed, used = match_windows(wanted, clients, described)
        emit("progress", stage="match", placed=len(claimed), total=total,
             message=f"{len(claimed)} of {total} windows already open")

        if not keep_others:
            ours = ancestor_pids()
            closing = [c for c in clients
                       if c["address"] not in used
                       and c.get("pid") not in ours
                       and (not ws_filter or workspace_keys(c["workspace"]["id"], c["workspace"]["name"]) & ws_filter)]
            for c in closing:
                dsp("window.close", window=window_sel(c["address"]))
            if closing:
                emit("progress", stage="close", placed=len(claimed), total=total,
                     message=f"Closed {len(closing)} other window{'s' if len(closing) != 1 else ''}")

        known = {c["address"] for c in clients}
        waiting = []
        for i, entry in enumerate(wanted):
            if i not in claimed and launch(entry):
                waiting.append(i)
        if waiting:
            emit("progress", stage="launch", placed=len(claimed), total=total,
                 message=f"Launching {len(waiting)} app{'s' if len(waiting) != 1 else ''}")

        deadline = time.time() + max(5, timeout)
        while waiting and time.time() < deadline:
            time.sleep(0.3)
            for c in live_clients():
                address = c["address"]
                if address in known or address in used or not waiting:
                    continue
                d = describe(c)
                pick = next((i for i in waiting if signature(wanted[i]) == signature(d)), None)
                if pick is None:
                    pick = next((i for i in waiting if wanted[i]["class"] and (
                        wanted[i]["class"] == d["class"] or wanted[i].get("initialClass") == d["initialClass"])), None)
                if pick is None:
                    continue
                claimed[pick] = address
                used.add(address)
                waiting.remove(pick)
                emit("progress", stage="launch", placed=len(claimed), total=total,
                     message=f"{wanted[pick]['class'] or 'Window'} is up")

        if len(used) > len(known & used):
            time.sleep(1.0)  # let freshly mapped windows settle before moving them

        monitors_now = {m.get("name") for m in (hyprctl_json("monitors") or [])}
        if ws_filter:
            active = hyprctl_json("activeworkspace") or {}
            focus_after = [workspace_selector(active.get("id"), active.get("name"))]
        else:
            saved = [m for m in template.get("monitors", []) if m.get("name") in monitors_now]
            focus_after = [m.get("activeWorkspace") for m in saved if not m.get("focused")]
            focus_after += [m.get("activeWorkspace") for m in saved if m.get("focused")]

        arrange(template, wanted, claimed, focus_after)

        missing = sorted({wanted[i]["class"] or wanted[i].get("title") or "unknown" for i in range(total) if i not in claimed})
        save_state(lastApplied=name)
        try:
            os.makedirs(RUNTIME_DIR, exist_ok=True)
            open(os.path.join(RUNTIME_DIR, "applied"), "w").close()
        except OSError:
            pass

        emit("done", name=name, placed=len(claimed), total=total, missing=missing)
        if show_notification:
            body = f"{len(claimed)} of {total} windows in place"
            if missing:
                body += "\nDidn't open: " + ", ".join(missing)
            notify(f"Restored “{name}”", body)
        return 0
    finally:
        lock.release()


# --------------------------------------------------------------------------- commands

def cmd_list(_args):
    session_info()
    templates = []
    for tpl in all_templates():
        windows = tpl.get("windows", [])
        templates.append({
            "name": tpl.get("name", ""),
            "savedAt": tpl.get("savedAt", ""),
            "windows": len(windows),
            "workspaces": workspace_labels(windows),
            "auto": bool(tpl.get("auto")),
        })
    templates.sort(key=lambda t: t["savedAt"], reverse=True)
    templates.sort(key=lambda t: t["auto"])

    clients = [c for c in live_clients() if (c.get("workspace") or {}).get("name") != STAGING]
    workspaces = {(c.get("workspace") or {}).get("id") for c in clients
                  if not str((c.get("workspace") or {}).get("name", "")).startswith("special:")}
    print(json.dumps({
        "templates": templates,
        "current": {"windows": len(clients), "workspaces": len(workspaces)},
        "lastApplied": load_state().get("lastApplied", ""),
    }))
    return 0


def cmd_save(args):
    name = args.name.strip()
    if not name:
        emit("error", message="Template name is empty")
        return 1
    data = snapshot(parse_workspace_filter(args.workspaces))
    if not data["windows"]:
        emit("error", message="There are no windows to save")
        return 1
    data["name"] = name
    data["auto"] = False
    write_json(template_path(name), data)
    emit("saved", name=name, windows=len(data["windows"]))
    return 0


def cmd_apply(args):
    return apply(args.name, keep_others=args.keep_others, timeout=args.timeout,
                 ws_filter=parse_workspace_filter(args.workspaces), show_notification=args.notify)


def cmd_delete(args):
    template = load_template(args.name)
    if not template:
        emit("error", message=f"No template named “{args.name}”")
        return 1
    os.remove(template["_path"])
    if load_state().get("lastApplied", "").lower() == args.name.lower():
        save_state(lastApplied="")
    emit("deleted", name=template.get("name", args.name))
    return 0


def cmd_autosave(_args):
    session = session_info()
    lock = RestoreLock()
    if not lock.acquire():
        return 0  # a restore is moving windows around right now
    lock.release()

    applied = os.path.exists(os.path.join(RUNTIME_DIR, "applied"))
    if not applied and time.time() - session.get("started", 0) < AUTOSAVE_GRACE_SECONDS:
        return 0  # give a fresh login time to be restored before snapshotting it

    data = snapshot()
    if not data["windows"]:
        return 0

    previous = load_template(AUTOSAVE_NAME)
    if previous and previous.get("sessionId") != session["id"]:
        previous.pop("_path", None)
        previous["name"] = PREVIOUS_NAME
        write_json(template_path(PREVIOUS_NAME), previous)

    data.update({"name": AUTOSAVE_NAME, "auto": True, "sessionId": session["id"]})
    write_json(template_path(AUTOSAVE_NAME), data)
    emit("saved", name=AUTOSAVE_NAME, windows=len(data["windows"]))
    return 0


def cmd_startup(args):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    try:
        os.close(os.open(os.path.join(RUNTIME_DIR, "startup-done"), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return 0  # already restored during this login

    for _ in range(60):
        if hyprctl_json("monitors"):
            break
        time.sleep(0.5)
    time.sleep(4)  # let autostart apps map first so they can be matched, not duplicated
    return cmd_apply(args)


def main():
    parser = argparse.ArgumentParser(description="Save and restore Hyprland window layouts.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    p = sub.add_parser("save")
    p.add_argument("name")
    p.add_argument("--workspaces", help="comma-separated workspaces to include (default: all)")
    p.add_argument("--no-commands", action="store_true",
                   help="record terminals as a bare shell, without what is running in them")
    p.set_defaults(func=cmd_save)

    for command, func in (("apply", cmd_apply), ("startup", cmd_startup)):
        p = sub.add_parser(command)
        p.add_argument("name")
        p.add_argument("--keep-others", action="store_true", help="don't close windows that aren't in the template")
        p.add_argument("--timeout", type=int, default=45, help="seconds to wait for launched apps")
        p.add_argument("--workspaces", help="comma-separated workspaces to restore (default: all)")
        p.add_argument("--notify", action="store_true", help="show a desktop notification when done")
        p.set_defaults(func=func)

    p = sub.add_parser("delete")
    p.add_argument("name")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("autosave")
    p.add_argument("--no-commands", action="store_true",
                   help="record terminals as a bare shell, without what is running in them")
    p.set_defaults(func=cmd_autosave)

    args = parser.parse_args()
    global CAPTURE_COMMANDS
    CAPTURE_COMMANDS = not getattr(args, "no_commands", False)
    try:
        sys.exit(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # surface failures to the panel instead of a silent exit
        emit("error", message=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
