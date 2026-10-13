#!/usr/bin/env python3
"""lyre -- a headless, CLI-driven audio engine for PipeWire.

Replaces EasyEffects with one native graph:

    apps ─▶ [per-app EQ]* ─▶ 🔥 lyre (null sink) ─▶ [per-output EQ] ─▶ device(s)

Every EQ is a native `module-filter-chain` (builtin biquad bands) hosted by this
daemon (a filter-chain only lives as long as the client that loaded it, so the
daemon keeps a `pw-cli -m` process alive per chain). The daemon reacts to apps &
devices appearing via libpulse events and keeps the graph correct. The only UI is
the CLI -- the notif-centre Sound menu shells commands at it over a unix socket.
"""
import os
import re
import sys
import json
import time
import queue
import signal
import socket
import threading
import subprocess

try:
    import pulsectl                      # only the daemon needs it; the CLI is socket-only
except ImportError:
    pulsectl = None

HOME = os.path.expanduser("~")
XDG = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
CFG = os.path.join(os.environ.get("XDG_CONFIG_HOME", f"{HOME}/.config"), "lyre")
STATE = os.path.join(CFG, "state.json")
SOCK = os.path.join(XDG, "lyre.sock")

LYRE = "lyre"                 # the central null-sink (merge point)
BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]   # 10-band ISO graphic EQ
NB = len(BANDS)
Q = 1.41
VIRTUAL = (LYRE, "easyeffects", "haku_combined")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def is_real(name):
    return not (name.startswith("eqout_") or name.startswith("eqapp_")
                or any(v in name for v in VIRTUAL))


def sanitize(dev):
    return re.sub(r"[^A-Za-z0-9]", "_", dev)[:48]


def clean_app_name(si):
    """Human key for an app stream: 'PipeWire ALSA [.sonora-wrapped]' -> 'Sonora'."""
    pl = si.proplist
    raw = pl.get("application.name") or ""
    m = re.search(r"\[([^\]]+)\]", raw)
    name = (m.group(1) if m else raw).strip(" .")
    name = re.sub(r"[-_]?wrapped$", "", name)
    if not name:
        name = pl.get("media.name") or si.name or "Audio"
    if name and name == name.lower():
        name = name.replace("-", " ").replace("_", " ").strip().title()
    return name or "Audio"


def flat(gains):
    return not gains or all(abs(g) < 0.05 for g in gains)


# ----------------------------- state -----------------------------
def load_state():
    try:
        with open(STATE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("outputs", [])      # active output device names
    s.setdefault("out_eq", {})       # dev -> [NB gains]
    s.setdefault("app_eq", {})       # app -> [NB gains]
    s.setdefault("app_route", {})    # app -> device name to OVERRIDE to (absent/"" = follow system = the lyre merge)
    return s


def save_state(s):
    try:
        os.makedirs(CFG, exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


# ----------------------- filter-chain EQ -------------------------
def eq_graph_args(node_name, gains):
    """SPA-JSON module args: a stereo 10-band graphic EQ filter-chain sink."""
    nodes, links = [], []
    for ch in ("L", "R"):
        for i, f in enumerate(BANDS):
            g = gains[i] if i < len(gains) else 0.0
            nodes.append(
                f'{{ type=builtin name=eq{ch}_{i} label=bq_peaking '
                f'control={{ "Freq"={f} "Q"={Q} "Gain"={g} }} }}'
            )
            if i > 0:
                links.append(f'{{ output="eq{ch}_{i-1}:Out" input="eq{ch}_{i}:In" }}')
    return (
        "{ "
        f'node.description="lyre eq {node_name}" '
        "filter.graph={ "
        f'nodes=[ {" ".join(nodes)} ] '
        f'links=[ {" ".join(links)} ] '
        f'inputs=[ "eqL_0:In" "eqR_0:In" ] '
        f'outputs=[ "eqL_{NB-1}:Out" "eqR_{NB-1}:Out" ] '
        "} "
        f'capture.props={{ node.name="{node_name}" media.class=Audio/Sink '
        "node.autoconnect=false audio.channels=2 audio.position=[FL FR] } "
        # node.autoconnect=false is CRITICAL: without it WirePlumber auto-links this chain's output
        # back into the lyre sink (it's the default), making lyre->monitor->eq->lyre a
        # feedback loop that kills playback. lyre links every edge explicitly, so no autoconnect.
        f'playback.props={{ node.name="{node_name}.out" node.passive=true node.autoconnect=false '
        "audio.channels=2 audio.position=[FL FR] } "
        "}"
    )


def node_id(node_name):
    out = sh("pw-cli", "ls", "Node").stdout
    cur = None
    for line in out.splitlines():
        m = re.match(r"\s*id (\d+),", line)
        if m:
            cur = m.group(1)
        if f'node.name = "{node_name}"' in line:
            return int(cur) if cur else None
    return None


class EqChain:
    """A hosted 10-band EQ filter-chain. Audio in at <name>:playback_*, out at <name>.out:output_*."""

    def __init__(self, node_name, gains=None):
        self.node_name = node_name
        self.gains = (list(gains) + [0.0] * NB)[:NB] if gains else [0.0] * NB
        self.proc = None
        self._nid = None                          # cached node id (avoid re-parsing `pw-cli ls Node`)

    def start(self):
        if self.alive():
            return
        args = eq_graph_args(self.node_name, self.gains)
        self.proc = subprocess.Popen(
            ["pw-cli", "-m", "load-module", "libpipewire-module-filter-chain", args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):                       # wait until the node is up (then cache its id)
            self._nid = node_id(self.node_name)
            if self._nid is not None:
                break
            time.sleep(0.05)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def nid(self):
        if self._nid is None:
            self._nid = node_id(self.node_name)
        return self._nid

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None
        self._nid = None

    def set_band(self, i, gain):
        if not (0 <= i < NB):
            return
        self.gains[i] = gain
        nid = self.nid()
        if nid is None:
            return
        sh("pw-cli", "set-param", str(nid), "Props",
           f'{{ params=[ "eqL_{i}:Gain" {gain} "eqR_{i}:Gain" {gain} ] }}')

    def apply_all(self):
        nid = self.nid()
        if nid is None:
            return
        params = " ".join(f'"eqL_{i}:Gain" {g} "eqR_{i}:Gain" {g}'
                          for i, g in enumerate(self.gains))
        sh("pw-cli", "set-param", str(nid), "Props", f"{{ params=[ {params} ] }}")


# ----------------------------- engine ----------------------------
class Engine:
    def __init__(self):
        # pulse connections are created in worker() -- libpulse's mainloop can only be driven from the
        # thread that created it, so ALL pulse access lives on the worker thread (see worker/submit).
        self.pa = None
        self._ev = None
        self.state = load_state()
        self.out_chains = {}       # dev -> EqChain  (lyre.monitor -> eq -> device)
        self.app_chains = {}       # appkey -> EqChain  (app -> eq -> target)
        self._app_link = {}        # appkey -> last eqapp.out target (skip re-linking a steady route)
        self._active_outs = set()  # devices currently wired into the fan-out (for delta applies)
        self.cmd_q = queue.Queue() # (parts, reply_queue) from the socket thread -> the worker thread
        self.lock = threading.RLock()

    def _open(self):
        self.pa = pulsectl.Pulse("lyre")
        self._ev = pulsectl.Pulse("lyre-events")
        self._ev.event_mask_set("sink", "source", "sink_input", "card", "server")
        self._ev.event_callback_set(lambda ev: (_ for _ in ()).throw(pulsectl.PulseLoopStop))

    def reconnect(self):
        for c in (self.pa, self._ev):
            try: c.close()
            except Exception: pass
        self._open()

    def sinks(self):
        return self.pa.sink_list()

    def real_sinks(self):
        return [s for s in self.sinks() if is_real(s.name)]

    def ensure_lyre(self):
        if not any(s.name == LYRE for s in self.sinks()):
            sh("pactl", "load-module", "module-null-sink", "media.class=Audio/Sink",
               f"sink_name={LYRE}", "channel_map=front-left,front-right")
            for _ in range(30):
                if any(s.name == LYRE for s in self.sinks()):
                    break
                time.sleep(0.1)

    def _monitor_links(self):
        out = sh("pw-link", "-l").stdout
        links, cur = [], None
        for line in out.splitlines():
            if line[:1] not in (" ", "\t"):
                cur = line.strip()
            else:
                s = line.strip()
                if cur and cur.startswith(LYRE + ":monitor") and s.startswith("|->"):
                    links.append((cur, s[3:].strip()))
        return links

    def ensure_out_eq(self, dev):
        name = "eqout_" + sanitize(dev)
        chain = self.out_chains.get(dev)
        if chain is None or not chain.alive():          # alive() is a cheap proc poll (no `pw-cli ls`)
            if chain:
                chain.stop()
            chain = EqChain(name, self.state["out_eq"].get(dev))
            chain.start()
            self.out_chains[dev] = chain
        return chain

    def apply_outputs(self, names, full=False):
        with self.lock:
            self._apply(names, full=full)

    def _unlink_out(self, dev):
        """Drop a device from the fan-out but KEEP its EQ chain alive, so re-selecting is instant."""
        chain = self.out_chains.get(dev)
        if not chain:
            return
        for ch in ("FL", "FR"):
            sh("pw-link", "-d", f"{LYRE}:monitor_{ch}", f"{chain.node_name}:playback_{ch}")
            sh("pw-link", "-d", f"{chain.node_name}.out:output_{ch}", f"{dev}:playback_{ch}")

    def _apply(self, names, full=False):
        """lyre.monitor -> [per-output EQ] -> each device. DELTA by default: only touch devices
        that actually changed, so toggling one output is fast and doesn't glitch the others (the old
        clear-everything-and-rebuild was the ~6s toggle + the re-poll of every EQ chain). full=True
        clears and rebuilds the whole fan-out (daemon start / device hotplug). Caller holds self.lock."""
        self.ensure_lyre()
        have = {s.name for s in self.real_sinks()}
        names = [n for n in names if n in have]
        if not names:
            first = next((s.name for s in self.real_sinks()), None)
            names = [first] if first else []
        want = set(names)
        if full:
            for mport, tgt in self._monitor_links():      # wipe the fan-out and rebuild from scratch
                sh("pw-link", "-d", mport, tgt)
            for dev in list(self.out_chains):             # drop chains for devices that are gone
                if dev not in have:
                    self.out_chains.pop(dev).stop()
            self._active_outs = set()
        have_now = self._active_outs
        for dev in have_now - want:                       # removed outputs: unlink (chain kept alive)
            self._unlink_out(dev)
        for dev in names:                                 # added outputs (all of them, on full)
            if dev in have_now and not full:
                continue
            sh("pactl", "suspend-sink", dev, "0")         # wake (idle sinks get dropped otherwise)
            chain = self.ensure_out_eq(dev)
            for ch in ("FL", "FR"):
                sh("pw-link", f"{LYRE}:monitor_{ch}", f"{chain.node_name}:playback_{ch}")
                sh("pw-link", f"{chain.node_name}.out:output_{ch}", f"{dev}:playback_{ch}")
        if os.environ.get("LYRE_NO_DEFAULT") != "1":   # test guard: don't hijack the live default
            sh("pactl", "set-default-sink", LYRE)
        sh("pactl", "set-sink-mute", LYRE, "0")
        self._active_outs = want
        self.state["outputs"] = names
        save_state(self.state)
        self._last_devs = frozenset(have)
        self.reconcile_apps()

    def set_out_eq(self, dev, band, gain):
        with self.lock:
            g = self.state["out_eq"].setdefault(dev, [0.0] * NB)
            if 0 <= band < NB:
                g[band] = gain
            chain = self.out_chains.get(dev)
            if chain:
                chain.set_band(band, gain)
            save_state(self.state)

    def set_out_eq_all(self, dev, gains):
        """Set every output band at once (one apply_all) -- used by presets."""
        with self.lock:
            g = (list(gains) + [0.0] * NB)[:NB]
            self.state["out_eq"][dev] = g
            chain = self.out_chains.get(dev)
            if chain:
                chain.gains = list(g)
                chain.apply_all()
            save_state(self.state)

    def reset_out_eq(self, dev):
        with self.lock:
            self.state["out_eq"][dev] = [0.0] * NB
            chain = self.out_chains.get(dev)
            if chain:
                chain.gains = [0.0] * NB
                chain.apply_all()
            save_state(self.state)

    # ---- per-app EQ: each EQ'd app -> its own chain -> lyre; flat apps go direct ----
    def _app_streams(self):
        out = []
        for si in self.pa.sink_input_list():
            nn = si.proplist.get("node.name") or ""
            if nn.startswith("eqapp_") or nn.startswith("eqout_"):   # skip our own chain streams
                continue
            out.append(si)
        return out

    def ensure_app_eq(self, key):
        name = "eqapp_" + sanitize(key)
        chain = self.app_chains.get(key)
        if chain is None or not chain.alive():
            if chain:
                chain.stop()
            chain = EqChain(name, self.state["app_eq"].get(key))
            chain.start()
            self.app_chains[key] = chain
            self._app_link.pop(key, None)             # new chain has no out links yet -> force a (re)link
        return chain

    def _out_links(self, node):
        """{port -> set(targets)} for node's output ports, parsed from `pw-link -l`."""
        out = sh("pw-link", "-l").stdout
        res, cur = {}, None
        for line in out.splitlines():
            if line[:1] not in (" ", "\t"):
                cur = line.strip()
            else:
                s = line.strip()
                if cur and cur.startswith(node + ":output_") and s.startswith("|->"):
                    res.setdefault(cur.split(":", 1)[1], set()).add(s[3:].strip())
        return res

    def _ensure_out_link(self, node, dst_sink):
        """Make node:output_{FL,FR} feed EXACTLY dst_sink:playback_{FL,FR}. Idempotent -- only changes
        what's wrong -- so a steady route doesn't re-link (and glitch) on every reconcile."""
        links = self._out_links(node)
        for ch in ("FL", "FR"):
            want = f"{dst_sink}:playback_{ch}"
            cur = links.get(f"output_{ch}", set())
            for t in cur:
                if t != want:
                    sh("pw-link", "-d", f"{node}:output_{ch}", t)
            if want not in cur:
                sh("pw-link", f"{node}:output_{ch}", want)

    def _route_target(self, key, have):
        """Where an app should land: an override device (if set + present), else the lyre merge."""
        dev = self.state["app_route"].get(key, "")
        return dev if (dev and dev in have) else LYRE

    def reconcile_apps(self):
        """Send each app to its route target -- an override device, else the lyre merge. Apps with a
        non-flat EQ pass through their own eqapp chain first (chain.out -> target); flat apps move
        straight to the target."""
        by_name = {s.name: s for s in self.sinks()}
        if LYRE not in by_name:
            return
        have = {s.name for s in self.real_sinks()}
        no_move = os.environ.get("LYRE_NO_MOVE") == "1"
        active = set()
        for si in self._app_streams():
            key = clean_app_name(si)
            gains = self.state["app_eq"].get(key)
            target = self._route_target(key, have)        # lyre sink name, or an override device
            if gains and not flat(gains):
                active.add(key)
                chain = self.ensure_app_eq(key)
                if self._app_link.get(key) != target:    # only (re)link when the route actually changed
                    self._ensure_out_link(chain.node_name + ".out", target)   # eqapp.out -> target
                    self._app_link[key] = target
                tgt = by_name.get(chain.node_name)
            else:
                tgt = by_name.get(target)
            if tgt and si.sink != tgt.index and not no_move:
                try: self.pa.sink_input_move(si.index, tgt.index)
                except Exception: pass
        for key in list(self.app_chains):                 # tear down chains with no live EQ'd app
            if key not in active:
                self.app_chains.pop(key).stop()
                self._app_link.pop(key, None)

    def set_app_eq(self, app, band, gain):
        with self.lock:
            g = self.state["app_eq"].setdefault(app, [0.0] * NB)
            if 0 <= band < NB:
                g[band] = gain
            if flat(g):
                self.state["app_eq"].pop(app, None)
            save_state(self.state)
            self.reconcile_apps()
            chain = self.app_chains.get(app)
            if chain and 0 <= band < NB:
                chain.set_band(band, gain)

    def set_app_eq_all(self, app, gains):
        """Set every app band at once (one reconcile + one apply_all) -- used by presets."""
        with self.lock:
            g = (list(gains) + [0.0] * NB)[:NB]
            if flat(g):
                self.state["app_eq"].pop(app, None)
            else:
                self.state["app_eq"][app] = g
            save_state(self.state)
            self.reconcile_apps()
            chain = self.app_chains.get(app)
            if chain:
                chain.gains = list(g)
                chain.apply_all()

    def reset_app_eq(self, app):
        with self.lock:
            self.state["app_eq"].pop(app, None)
            save_state(self.state)
            self.reconcile_apps()

    def set_app_route(self, app, dev):
        """Pin an app's output: dev="" (or "system") = follow the lyre merge; else override to dev."""
        with self.lock:
            if dev:
                self.state["app_route"][app] = dev
            else:
                self.state["app_route"].pop(app, None)
            save_state(self.state)
            self.reconcile_apps()

    def status(self):
        try:
            default = self.pa.server_info().default_sink_name
        except Exception:
            default = ""
        return {
            "lyre": any(s.name == LYRE for s in self.sinks()),
            "default": default,
            "outputs": self.state["outputs"],
            "devices": [{"name": s.name, "description": s.description} for s in self.real_sinks()],
            "bands": BANDS,
            "out_eq": self.state["out_eq"],
            "apps": sorted(set(clean_app_name(si) for si in self._app_streams())),
            "app_eq": self.state["app_eq"],
            "app_route": self.state["app_route"],
        }

    def reconcile(self):
        """Re-apply ONLY when the graph actually needs it (device set changed, a chain died,
        or the lyre sink vanished) -- reading state inside the lock so we never re-apply a
        stale outputs list, and never churn links on every event (which would glitch audio)."""
        with self.lock:
            try:
                devs = frozenset(s.name for s in self.real_sinks())
                ok = (any(s.name == LYRE for s in self.sinks())
                      and self.out_chains
                      and all(c.alive() for c in self.out_chains.values()))
                if ok and devs == getattr(self, "_last_devs", None):
                    self.reconcile_apps()          # apps come/go without a device change
                    return
                self._apply(list(self.state.get("outputs") or []), full=True)
            except Exception:
                self.reconnect()

    def cleanup_orphans(self):
        """Kill eqout_/eqapp_ filter-chain hosts left by a previous daemon instance (they survive as
        detached processes) and unload stale lyre null-sinks, so chains don't pile up across
        restarts (the pile-up tangles the graph: several nodes share a name, links hit the wrong one).
        Safe at startup -- this daemon has no children yet, so every such host is an orphan."""
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                if "pw-cli" in cmd and "module-filter-chain" in cmd and ("eqout_" in cmd or "eqapp_" in cmd):
                    try: os.kill(int(pid), signal.SIGKILL)
                    except Exception: pass
        except Exception:
            pass
        for line in sh("pactl", "list", "short", "modules").stdout.splitlines():
            if "module-null-sink" in line and f"sink_name={LYRE}" in line:
                mid = line.split()[0] if line.split() else ""
                if mid:
                    sh("pactl", "unload-module", mid)

    def start(self):
        self.cleanup_orphans()
        self.ensure_lyre()
        want = self.state["outputs"]
        if not want:
            first = next((s.name for s in self.real_sinks()), None)
            want = [first] if first else []
        self.apply_outputs(want, full=True)

    # -------- the single pulse thread: owns both connections, runs commands + reconciles --------
    def worker(self):
        self._open()
        self.start()
        self._drain()
        while True:
            try:
                self._ev.event_listen(timeout=0.4)        # short: also the command-queue poll granularity
            except pulsectl.PulseLoopStop:
                pass
            except Exception:
                try: self.reconnect()
                except Exception: pass
                self._drain()
                continue
            self._drain()                                 # commands first (likely why we woke)
            try:
                self.reconcile()
            except Exception:
                try: self.reconnect()
                except Exception: pass

    def _drain(self):
        while not self.cmd_q.empty():
            parts, rq = self.cmd_q.get()
            rq.put(self._dispatch(parts))

    def submit(self, parts, timeout=15):
        """Socket thread -> worker thread: enqueue a command, await its reply. Deliberately touches NO
        pulse object (the worker owns both connections); the worker's short event_listen timeout is the
        poll that picks this up within a fraction of a second."""
        rq = queue.Queue()
        self.cmd_q.put((parts, rq))
        try:
            return rq.get(timeout=timeout)
        except Exception:
            return "err timeout"

    def _dispatch(self, parts):                           # runs ON the worker thread (all pulse access safe)
        if not parts:
            return "err empty"
        cmd = parts[0]
        try:
            if cmd == "status":
                return json.dumps(self.status())
            if cmd == "ping":
                return "pong"
            if cmd == "outputs":
                self.apply_outputs(parts[1:])
                return "ok"
            if cmd == "route" and len(parts) >= 3:        # route <app> <device>|system (per-app output override)
                dev = parts[2]
                self.set_app_route(parts[1], "" if dev in ("system", "") else dev)
                return "ok"
            if cmd == "eq" and len(parts) >= 3 and parts[1] in ("out", "app"):
                scope, target = parts[1], parts[2]
                if len(parts) >= 4 and parts[3] == "reset":
                    (self.reset_out_eq if scope == "out" else self.reset_app_eq)(target)
                    return "ok"
                if len(parts) >= 4 and parts[3] == "all":  # bulk set (presets): eq <scope> <t> all g0..g9
                    gains = [float(x) for x in parts[4:4 + NB]]
                    (self.set_out_eq_all if scope == "out" else self.set_app_eq_all)(target, gains)
                    return "ok"
                band, gain = int(parts[3]), float(parts[4])
                (self.set_out_eq if scope == "out" else self.set_app_eq)(target, band, gain)
                return "ok"
            return f"err unknown: {parts}"
        except Exception as e:
            return f"err {e}"


# ------------------------- daemon + CLI --------------------------
def run_daemon():
    engine = Engine()
    threading.Thread(target=engine.worker, daemon=True).start()
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        try:
            data = conn.recv(4096).decode("utf-8", "replace")
            try:
                parts = json.loads(data)          # args arrive JSON-encoded (multi-word app names survive)
            except Exception:
                parts = data.strip().split()
            reply = engine.submit(parts)          # hand to the pulse thread, await its reply
            conn.sendall((reply + "\n").encode())
        except Exception:
            pass
        finally:
            conn.close()


def run_cli(args):
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(SOCK)
        c.sendall(json.dumps(args).encode())
        print(c.recv(1 << 20).decode("utf-8", "replace").rstrip())
        c.close()
        return 0
    except (FileNotFoundError, ConnectionRefusedError):
        sys.stderr.write("lyre: daemon not running\n")
        return 1


def main():
    args = sys.argv[1:]
    if not args or args[0] == "daemon":
        run_daemon()
        return 0
    return run_cli(args)


if __name__ == "__main__":
    main()
