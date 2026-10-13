#!/usr/bin/env python3
"""bonfire -- a headless, CLI-driven audio engine for PipeWire.

Replaces EasyEffects with one native graph:

    apps ─▶ [per-app EQ]* ─▶ 🔥 bonfire (null sink) ─▶ [per-output EQ] ─▶ device(s)

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
import socket
import threading
import subprocess

try:
    import pulsectl                      # only the daemon needs it; the CLI is socket-only
except ImportError:
    pulsectl = None

HOME = os.path.expanduser("~")
XDG = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
CFG = os.path.join(os.environ.get("XDG_CONFIG_HOME", f"{HOME}/.config"), "bonfire")
STATE = os.path.join(CFG, "state.json")
SOCK = os.path.join(XDG, "bonfire.sock")

BONFIRE = "bonfire"                 # the central null-sink (merge point)
BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]   # 10-band ISO graphic EQ
NB = len(BANDS)
Q = 1.41
VIRTUAL = (BONFIRE, "easyeffects", "haku_combined")


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
    s.setdefault("app_eq", {})       # app -> [NB gains]  (phase 2)
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
        f'node.description="bonfire eq {node_name}" '
        "filter.graph={ "
        f'nodes=[ {" ".join(nodes)} ] '
        f'links=[ {" ".join(links)} ] '
        f'inputs=[ "eqL_0:In" "eqR_0:In" ] '
        f'outputs=[ "eqL_{NB-1}:Out" "eqR_{NB-1}:Out" ] '
        "} "
        f'capture.props={{ node.name="{node_name}" media.class=Audio/Sink '
        "audio.channels=2 audio.position=[FL FR] } "
        f'playback.props={{ node.name="{node_name}.out" node.passive=true '
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


def node_exists(node_name):
    return node_id(node_name) is not None


class EqChain:
    """A hosted 10-band EQ filter-chain. Audio in at <name>:playback_*, out at <name>.out:output_*."""

    def __init__(self, node_name, gains=None):
        self.node_name = node_name
        self.gains = (list(gains) + [0.0] * NB)[:NB] if gains else [0.0] * NB
        self.proc = None

    def start(self):
        if self.alive():
            return
        args = eq_graph_args(self.node_name, self.gains)
        self.proc = subprocess.Popen(
            ["pw-cli", "-m", "load-module", "libpipewire-module-filter-chain", args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(40):                       # wait until the node is up
            if node_exists(self.node_name):
                break
            time.sleep(0.05)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None

    def set_band(self, i, gain):
        if not (0 <= i < NB):
            return
        self.gains[i] = gain
        nid = node_id(self.node_name)
        if nid is None:
            return
        sh("pw-cli", "set-param", str(nid), "Props",
           f'{{ params=[ "eqL_{i}:Gain" {gain} "eqR_{i}:Gain" {gain} ] }}')

    def apply_all(self):
        nid = node_id(self.node_name)
        if nid is None:
            return
        params = " ".join(f'"eqL_{i}:Gain" {g} "eqR_{i}:Gain" {g}'
                          for i, g in enumerate(self.gains))
        sh("pw-cli", "set-param", str(nid), "Props", f"{{ params=[ {params} ] }}")


# ----------------------------- engine ----------------------------
class Engine:
    def __init__(self):
        self.pa = pulsectl.Pulse("bonfire")
        self.state = load_state()
        self.out_chains = {}       # dev -> EqChain  (bonfire.monitor -> eq -> device)
        self.app_chains = {}       # appkey -> EqChain  (app -> eq -> bonfire)
        self.lock = threading.RLock()

    def reconnect(self):
        try:
            self.pa.close()
        except Exception:
            pass
        self.pa = pulsectl.Pulse("bonfire")

    def sinks(self):
        return self.pa.sink_list()

    def real_sinks(self):
        return [s for s in self.sinks() if is_real(s.name)]

    def ensure_bonfire(self):
        if not any(s.name == BONFIRE for s in self.sinks()):
            sh("pactl", "load-module", "module-null-sink", "media.class=Audio/Sink",
               f"sink_name={BONFIRE}", "channel_map=front-left,front-right")
            for _ in range(30):
                if any(s.name == BONFIRE for s in self.sinks()):
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
                if cur and cur.startswith(BONFIRE + ":monitor") and s.startswith("|->"):
                    links.append((cur, s[3:].strip()))
        return links

    def ensure_out_eq(self, dev):
        name = "eqout_" + sanitize(dev)
        chain = self.out_chains.get(dev)
        if chain is None or not chain.alive() or not node_exists(name):
            if chain:
                chain.stop()
            chain = EqChain(name, self.state["out_eq"].get(dev))
            chain.start()
            self.out_chains[dev] = chain
        return chain

    def apply_outputs(self, names):
        with self.lock:
            self._apply(names)

    def _apply(self, names):
        """bonfire.monitor -> [per-output EQ] -> each device. Caller holds self.lock."""
        self.ensure_bonfire()
        have = {s.name for s in self.real_sinks()}
        names = [n for n in names if n in have]
        if not names:
            first = next((s.name for s in self.real_sinks()), None)
            names = [first] if first else []
        for mport, tgt in self._monitor_links():         # clear old fan-out
            sh("pw-link", "-d", mport, tgt)
        for dev in list(self.out_chains):                # drop EQ chains for dropped outputs
            if dev not in names:
                self.out_chains.pop(dev).stop()
        for dev in names:
            sh("pactl", "suspend-sink", dev, "0")        # wake
            chain = self.ensure_out_eq(dev)
            eq = chain.node_name
            for ch in ("FL", "FR"):
                sh("pw-link", f"{BONFIRE}:monitor_{ch}", f"{eq}:playback_{ch}")
                sh("pw-link", f"{eq}.out:output_{ch}", f"{dev}:playback_{ch}")
        if os.environ.get("BONFIRE_NO_DEFAULT") != "1":   # test guard: don't hijack the live default
            sh("pactl", "set-default-sink", BONFIRE)
        sh("pactl", "set-sink-mute", BONFIRE, "0")
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

    def reset_out_eq(self, dev):
        with self.lock:
            self.state["out_eq"][dev] = [0.0] * NB
            chain = self.out_chains.get(dev)
            if chain:
                chain.gains = [0.0] * NB
                chain.apply_all()
            save_state(self.state)

    # ---- per-app EQ: each EQ'd app -> its own chain -> bonfire; flat apps go direct ----
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
        if chain is None or not chain.alive() or not node_exists(name):
            if chain:
                chain.stop()
            chain = EqChain(name, self.state["app_eq"].get(key))
            chain.start()
            self.app_chains[key] = chain
            for ch in ("FL", "FR"):                       # chain output -> bonfire input
                sh("pw-link", f"{name}.out:output_{ch}", f"{BONFIRE}:playback_{ch}")
        return chain

    def reconcile_apps(self):
        """Route each EQ'd app through its per-app chain into the bonfire; flat apps go straight in."""
        by_name = {s.name: s for s in self.sinks()}
        bf = by_name.get(BONFIRE)
        if not bf:
            return
        active = set()
        for si in self._app_streams():
            key = clean_app_name(si)
            gains = self.state["app_eq"].get(key)
            if gains and not flat(gains):
                active.add(key)
                chain = self.ensure_app_eq(key)
                tgt = by_name.get(chain.node_name)
                if tgt and si.sink != tgt.index and os.environ.get("BONFIRE_NO_MOVE") != "1":
                    try: self.pa.sink_input_move(si.index, tgt.index)
                    except Exception: pass
            elif si.sink != bf.index and os.environ.get("BONFIRE_NO_MOVE") != "1":
                try: self.pa.sink_input_move(si.index, bf.index)
                except Exception: pass
        for key in list(self.app_chains):                 # tear down chains with no live EQ'd app
            if key not in active:
                self.app_chains.pop(key).stop()

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

    def reset_app_eq(self, app):
        with self.lock:
            self.state["app_eq"].pop(app, None)
            save_state(self.state)
            self.reconcile_apps()

    def status(self):
        try:
            default = self.pa.server_info().default_sink_name
        except Exception:
            default = ""
        return {
            "bonfire": any(s.name == BONFIRE for s in self.sinks()),
            "default": default,
            "outputs": self.state["outputs"],
            "devices": [{"name": s.name, "description": s.description} for s in self.real_sinks()],
            "bands": BANDS,
            "out_eq": self.state["out_eq"],
            "apps": sorted(set(clean_app_name(si) for si in self._app_streams())),
            "app_eq": self.state["app_eq"],
        }

    def reconcile(self):
        """Re-apply ONLY when the graph actually needs it (device set changed, a chain died,
        or the bonfire sink vanished) -- reading state inside the lock so we never re-apply a
        stale outputs list, and never churn links on every event (which would glitch audio)."""
        with self.lock:
            try:
                devs = frozenset(s.name for s in self.real_sinks())
                ok = (any(s.name == BONFIRE for s in self.sinks())
                      and self.out_chains
                      and all(c.alive() for c in self.out_chains.values()))
                if ok and devs == getattr(self, "_last_devs", None):
                    self.reconcile_apps()          # apps come/go without a device change
                    return
                self._apply(list(self.state.get("outputs") or []))
            except Exception:
                self.reconnect()

    def start(self):
        self.ensure_bonfire()
        want = self.state["outputs"]
        if not want:
            first = next((s.name for s in self.real_sinks()), None)
            want = [first] if first else []
        self.apply_outputs(want)


# ------------------------- daemon + CLI --------------------------
def handle(engine, line):
    try:
        parts = json.loads(line)                 # args arrive JSON-encoded (multi-word app names survive)
    except Exception:
        parts = line.strip().split()
    if not parts:
        return "err empty"
    cmd = parts[0]
    try:
        if cmd == "status":
            return json.dumps(engine.status())
        if cmd == "ping":
            return "pong"
        if cmd == "outputs":
            engine.apply_outputs(parts[1:])
            return "ok"
        if cmd == "eq" and len(parts) >= 3 and parts[1] in ("out", "app"):
            scope, target = parts[1], parts[2]
            if len(parts) >= 4 and parts[3] == "reset":
                (engine.reset_out_eq if scope == "out" else engine.reset_app_eq)(target)
                return "ok"
            band, gain = int(parts[3]), float(parts[4])
            (engine.set_out_eq if scope == "out" else engine.set_app_eq)(target, band, gain)
            return "ok"
        return f"err unknown: {parts}"
    except Exception as e:
        return f"err {e}"


def events_thread(engine):
    while True:
        try:
            p = pulsectl.Pulse("bonfire-events")
            p.event_mask_set("sink", "source", "sink_input", "card", "server")
            p.event_callback_set(lambda ev: (_ for _ in ()).throw(pulsectl.PulseLoopStop))
            while True:
                p.event_listen(timeout=3)
                engine.reconcile()
        except Exception:
            time.sleep(1.5)


def run_daemon():
    engine = Engine()
    engine.start()
    threading.Thread(target=events_thread, args=(engine,), daemon=True).start()
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
            reply = handle(engine, data)
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
        sys.stderr.write("bonfire: daemon not running\n")
        return 1


def main():
    args = sys.argv[1:]
    if not args or args[0] == "daemon":
        run_daemon()
        return 0
    return run_cli(args)


if __name__ == "__main__":
    main()
