#!/usr/bin/env python3
"""
blurb-daemon.py
Long-lived daemon that mirrors an agent pane's terminal title (OSC 0/2) into its Herdr pane label.

Responsibilities:
  - Subscribe to `pane.updated` on the Herdr socket API and track agent panes.
  - Derive a pane label from the pane's stripped terminal title, filtered and formatted per config.
  - Apply labels via `pane.rename`, idempotently, without clobbering labels a human set.
  - Persist ownership state so restarts neither duplicate work nor steal manual labels.
  - Provide start/stop/status/resync/clear subcommands for the plugin's manifest actions.
"""

import errno
import json
import os
import re
import select
import signal
import socket
import sys
import time
import tomllib

DEFAULTS = {
    # Canonical agent ids to act on. Empty list means every detected agent.
    "agents": ["claude"],
    # Exact stripped titles that carry no information; never promoted to a label.
    "ignore_titles": ["Claude Code", "Codex", "opencode"],
    # Regexes for titles that are shell chrome rather than agent status.
    "ignore_patterns": [r"^[\w.-]+@[\w.-]+:", r"^~?/", r"^\$ "],
    # Keep the previous label when the current title is ignored, instead of clearing.
    "retain_on_ignore": True,
    # Adopt panes that already carry a label this plugin did not set.
    "claim_foreign_labels": False,
    # Drop the label when the agent exits the pane.
    "clear_on_agent_exit": False,
    # Label template. Tokens: {title} {agent} {status} {cwd_base}
    "template": "{title}",
    # Truncated with an ellipsis beyond this many characters. 0 disables truncation.
    "max_length": 64,
    # Seconds to coalesce rapid title changes before applying a label.
    "debounce_seconds": 0.35,
    # Reconnect attempts after the socket drops before the daemon gives up.
    "reconnect_attempts": 5,
}

ELLIPSIS = "…"


def plugin_root():
    return os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )


def plugin_id():
    """Read the id from the manifest so CLI runs share the daemon's directories."""
    env_id = os.environ.get("HERDR_PLUGIN_ID")
    if env_id:
        return env_id
    try:
        with open(os.path.join(plugin_root(), "herdr-plugin.toml"), "rb") as handle:
            return tomllib.load(handle)["id"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        raise SystemExit("cannot determine plugin id: no HERDR_PLUGIN_ID and no readable manifest")


def state_dir():
    # Mirrors Herdr's managed layout so `blurb-daemon.py status` run by hand
    # inspects the same state the daemon writes.
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
        os.path.join("~/.local/state/herdr/plugins", plugin_id())
    )
    os.makedirs(path, exist_ok=True)
    return path


def config_dir():
    path = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(
        os.path.join("~/.config/herdr/plugins/config", plugin_id())
    )
    os.makedirs(path, exist_ok=True)
    return path


def socket_path():
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        default = os.path.expanduser("~/.config/herdr/herdr.sock")
        path = default if os.path.exists(default) else None
    if not path:
        raise SystemExit("HERDR_SOCKET_PATH is unset and no default socket was found")
    return path


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(config_dir(), "config.toml")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            user = tomllib.load(handle)
        unknown = sorted(set(user) - set(DEFAULTS))
        for key, value in user.items():
            if key in cfg:
                cfg[key] = value
        cfg["_unknown_keys"] = unknown
    cfg["_ignore_res"] = [re.compile(p) for p in cfg["ignore_patterns"]]
    return cfg


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
    try:
        with open(os.path.join(state_dir(), "blurb.log"), "a") as handle:
            handle.write(line)
    except OSError:
        pass


# --- socket helpers ---------------------------------------------------------


def call(path, method, params=None, timeout=5.0):
    """Issue one request and return the response.

    The Herdr socket API serves a single request per connection and then closes,
    so every call gets its own short-lived socket. Only subscriptions stay open.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        payload = {"id": "cc-blurb", "method": method, "params": params or {}}
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        raw = buf.split(b"\n", 1)[0]
        if not raw.strip():
            return None
        response = json.loads(raw)
        # An error reply is a well-formed message; callers must not read it as success.
        if "error" in response or "result" not in response:
            log("call %s rejected: %s" % (method, json.dumps(response)[:200]))
            return None
        return response
    except (OSError, json.JSONDecodeError) as exc:
        log("call %s failed: %s" % (method, exc))
        return None
    finally:
        sock.close()


class Client:
    """One connection to the Herdr socket API, speaking newline-delimited JSON."""

    def __init__(self, path, timeout=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.seq = 0

    def send(self, method, params=None):
        self.seq += 1
        request_id = "cc-blurb:%d" % self.seq
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.sock.sendall((json.dumps(payload) + "\n").encode())
        return request_id

    def drain(self):
        """Yield whole JSON messages already sitting in the buffer."""
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            if raw.strip():
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue

    def poll(self, timeout):
        """Wait up to `timeout` for readable data. Returns False when the peer closed."""
        readable, _, _ = select.select([self.sock], [], [], timeout)
        if not readable:
            return True
        chunk = self.sock.recv(65536)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- label logic ------------------------------------------------------------


def owned_path():
    return os.path.join(state_dir(), "owned.json")


def load_owned():
    try:
        with open(owned_path()) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_owned(owned):
    tmp = owned_path() + ".tmp"
    try:
        with open(tmp, "w") as handle:
            json.dump(owned, handle)
        os.replace(tmp, owned_path())
    except OSError:
        pass


def agent_matches(cfg, pane):
    agents = cfg["agents"]
    agent = pane.get("agent")
    if not agent:
        return False
    return not agents or agent in agents


def title_is_noise(cfg, title):
    if not title:
        return True
    if title in cfg["ignore_titles"]:
        return True
    return any(rx.search(title) for rx in cfg["_ignore_res"])


def render_label(cfg, pane, title):
    cwd = pane.get("foreground_cwd") or pane.get("cwd") or ""
    label = cfg["template"].format(
        title=title,
        agent=pane.get("agent") or "",
        status=pane.get("agent_status") or "",
        cwd_base=os.path.basename(cwd.rstrip("/")) if cwd else "",
    )
    label = " ".join(label.split())
    limit = cfg["max_length"]
    if limit and len(label) > limit:
        label = label[: max(1, limit - 1)].rstrip() + ELLIPSIS
    return label


def desired_label(cfg, pane, owned):
    """Return (action, label). action is 'set', 'clear', or 'skip'."""
    pane_id = pane.get("pane_id")
    if not pane_id:
        return "skip", None

    current = pane.get("label")
    ours = owned.get(pane_id)
    # A label we did not write is a human's; leave it alone.
    if current and current != ours and not cfg["claim_foreign_labels"]:
        return "skip", None

    if not agent_matches(cfg, pane):
        if ours and cfg["clear_on_agent_exit"]:
            return "clear", None
        return "skip", None

    title = (pane.get("terminal_title_stripped") or "").strip()
    if title_is_noise(cfg, title):
        if ours and not cfg["retain_on_ignore"]:
            return "clear", None
        return "skip", None

    label = render_label(cfg, pane, title)
    if not label or label == ours == current:
        return "skip", None
    return "set", label


def apply_label(path, cfg, pane, owned):
    action, label = desired_label(cfg, pane, owned)
    if action == "skip":
        return False
    pane_id = pane["pane_id"]
    if call(path, "pane.rename", {"pane_id": pane_id, "label": label}) is None:
        return False
    if action == "clear":
        owned.pop(pane_id, None)
    else:
        owned[pane_id] = label
    return True


def sweep(path, cfg, owned):
    """Label every currently known pane. Used at startup and by the resync action."""
    response = call(path, "pane.list", {})
    panes = ((response or {}).get("result") or {}).get("panes") or []
    changed = 0
    for pane in panes:
        if apply_label(path, cfg, pane, owned):
            changed += 1
    if changed:
        save_owned(owned)
    return changed, len(panes)


# --- pidfile ----------------------------------------------------------------


def pid_path():
    return os.path.join(state_dir(), "daemon.pid")


def running_pid():
    try:
        with open(pid_path()) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        if exc.errno != errno.EPERM:
            return None
    return pid


# --- daemon loop ------------------------------------------------------------


def run_loop():
    cfg = load_config()
    path = socket_path()
    owned = load_owned()
    attempts = cfg["reconnect_attempts"]

    while attempts >= 0:
        try:
            client = Client(path, timeout=None)
        except OSError as exc:
            attempts -= 1
            log("connect failed (%s); %d attempts left" % (exc, max(attempts, 0)))
            time.sleep(1.0)
            continue

        try:
            changed, total = sweep(path, cfg, owned)
            log("sweep labelled %d of %d panes" % (changed, total))
            client.send("events.subscribe", {"subscriptions": [{"type": "pane.updated"}]})

            pending = {}
            debounce = cfg["debounce_seconds"]
            alive = True
            while alive:
                # Wake on data, or on the debounce tick so a final title change
                # still lands when no further events arrive.
                alive = client.poll(debounce)
                for message in client.drain():
                    if message.get("event") != "pane_updated":
                        continue
                    pane = ((message.get("data") or {}).get("pane")) or {}
                    if pane.get("pane_id"):
                        pending[pane["pane_id"]] = (pane, time.monotonic())

                now = time.monotonic()
                dirty = False
                for key in [k for k, (_, at) in pending.items() if now - at >= debounce]:
                    staged, _ = pending.pop(key)
                    if apply_label(path, cfg, staged, owned):
                        dirty = True
                if dirty:
                    save_owned(owned)
        except OSError as exc:
            log("stream error: %s" % exc)
        finally:
            client.close()

        attempts -= 1
        if attempts >= 0:
            log("reconnecting; %d attempts left" % attempts)
            time.sleep(1.0)

    log("giving up after reconnect attempts exhausted")


def daemonize():
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    with open(pid_path(), "w") as handle:
        handle.write(str(os.getpid()))


# --- subcommands ------------------------------------------------------------


def cmd_start(_args):
    pid = running_pid()
    if pid:
        print("cc-blurb already running (pid %d)" % pid)
        return 0
    daemonize()
    try:
        run_loop()
    finally:
        try:
            os.unlink(pid_path())
        except OSError:
            pass
    return 0


def cmd_stop(_args):
    pid = running_pid()
    if not pid:
        print("cc-blurb not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("stopped cc-blurb (pid %d)" % pid)
    return 0


def cmd_status(_args):
    pid = running_pid()
    cfg = load_config()
    owned = load_owned()
    print("daemon:  %s" % ("running (pid %d)" % pid if pid else "stopped"))
    print("socket:  %s" % socket_path())
    print("config:  %s" % os.path.join(config_dir(), "config.toml"))
    print("state:   %s" % state_dir())
    print("agents:  %s" % (", ".join(cfg["agents"]) or "<all>"))
    print("labeled: %d pane(s)" % len(owned))
    for pane_id, label in sorted(owned.items()):
        print("  %-8s %s" % (pane_id, label))
    if cfg.get("_unknown_keys"):
        print("unknown config keys: %s" % ", ".join(cfg["_unknown_keys"]))
    return 0


def cmd_resync(_args):
    cfg = load_config()
    owned = load_owned()
    changed, total = sweep(socket_path(), cfg, owned)
    print("relabelled %d of %d pane(s)" % (changed, total))
    return 0


def cmd_clear(_args):
    owned = load_owned()
    if not owned:
        print("no labels to clear")
        return 0
    path = socket_path()
    for pane_id in list(owned):
        call(path, "pane.rename", {"pane_id": pane_id, "label": None})
    count = len(owned)
    save_owned({})
    print("cleared %d label(s)" % count)
    return 0


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "resync": cmd_resync,
    "clear": cmd_clear,
    "run": lambda _args: (run_loop(), 0)[1],
}


def main(argv):
    name = argv[1] if len(argv) > 1 else "start"
    handler = COMMANDS.get(name)
    if not handler:
        print("usage: blurb-daemon.py {%s}" % "|".join(COMMANDS), file=sys.stderr)
        return 2
    return handler(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
