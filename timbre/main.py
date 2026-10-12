#!/usr/bin/env python3
"""Timbre -- a SoundSource-style audio control panel for PipeWire.

v2: event-driven (no polling jank), libadwaita UI, per-app output routing,
per-device gain, multi-device output. Audio via pulsectl (libpulse ->
pipewire-pulse); multi-device output is a persistent null-sink "router" fanned
out to devices with pw-link -- no module-combine-sink, so stream-restore can't
scramble it into an EasyEffects feedback loop. `timbre --apply` wires the
pipeline headlessly (used by the login service).
"""
import re
import time
import threading
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk  # noqa: E402

import pulsectl  # noqa: E402

ROUTER = "haku_combined"               # the persistent null-sink everything funnels through
VIRTUAL = ("easyeffects", ROUTER)
EE_SINK = "easyeffects_sink"
import os  # noqa: E402

STATE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                     "timbre", "outputs")


def is_real(name):
    return not any(v in name for v in VIRTUAL)


def clean_app_name(si):
    pl = si.proplist
    raw = pl.get("application.name") or ""
    m = re.search(r"\[([^\]]+)\]", raw)        # "PipeWire ALSA [.sonora-wrapped]"
    name = (m.group(1) if m else raw).strip(" .")
    name = re.sub(r"[-_]?wrapped$", "", name)
    if not name:
        name = pl.get("media.name") or si.name or "Audio"
    if name and name == name.lower():
        name = name.replace("-", " ").replace("_", " ").strip().title()
    return name or "Audio"


def app_icon_name(si, theme):
    pl = si.proplist
    cands = [pl.get("application.icon_name"), pl.get("application.process.binary")]
    raw = pl.get("application.name") or ""
    m = re.search(r"\[([^\]]+)\]", raw)
    base = (m.group(1) if m else raw).strip(" .")
    base = re.sub(r"[-_]?wrapped$", "", base)
    if base:
        cands += [base, base.lower(), base.split()[0].lower()]
    for c in cands:
        if c and theme.has_icon(c):
            return c
    return "application-x-executable-symbolic"


CSS = b"""
.pct { opacity:.55; font-variant-numeric:tabular-nums; min-width:40px; }
.audio-row { padding: 4px 2px; }
.dim { opacity:.55; font-size:.84em; }
scale { min-width: 150px; }
.app-icon { -gtk-icon-size: 22px; opacity:.85; }
"""


class PA:
    """pulsectl wrapper (main thread) + pactl for the combine module."""

    def __init__(self):
        self.p = pulsectl.Pulse("timbre")

    def reconnect(self):
        try:
            self.p.close()
        except Exception:
            pass
        self.p = pulsectl.Pulse("timbre")

    def sinks(self):
        return self.p.sink_list()

    def real_sinks(self):
        return [s for s in self.sinks() if is_real(s.name)]

    def apps(self):
        out = []
        for si in self.p.sink_input_list():
            mn = si.proplist.get("media.name") or ""
            nn = si.proplist.get("node.name") or ""
            if mn == ROUTER + " output" or nn.startswith("output." + ROUTER):
                continue
            out.append(si)
        return out

    def default_name(self):
        try:
            return self.p.server_info().default_sink_name
        except Exception:
            return ""

    def vol(self, obj):
        try:
            return self.p.volume_get_all_chans(obj)
        except Exception:
            return 0.0

    def set_vol(self, obj, frac):
        self.p.volume_set_all_chans(obj, max(0.0, min(1.0, frac)))

    def set_mute(self, obj, m):
        self.p.mute(obj, bool(m))

    def move_app(self, app, sink_index):
        self.p.sink_input_move(app.index, sink_index)

    def name_by_index(self):
        return {s.index: s for s in self.sinks()}

    # --- router: a persistent null-sink fanned out to devices via pw-link ---
    # No module-combine-sink anywhere: graph links can't be scrambled by stream-restore,
    # so the EE->combine->EE feedback loop that used to break playback cannot recur. One
    # device or many is the same operation (just more links).
    def router_exists(self):
        return any(s.name == ROUTER for s in self.sinks())

    def ensure_router(self):
        if not self.router_exists():
            subprocess.run(["pactl", "load-module", "module-null-sink",
                            "media.class=Audio/Sink", f"sink_name={ROUTER}",
                            "channel_map=front-left,front-right"], capture_output=True)
            for _ in range(25):
                if self.router_exists():
                    break
                time.sleep(0.1)

    # back-compat names the UI already calls:
    def combine_active(self):
        return self.router_exists()

    def combine_slaves(self):
        return sorted(self.router_targets())

    def router_links(self):
        """[(monitor_port, target_port)] currently linked out of the router monitor."""
        out = subprocess.run(["pw-link", "-l"], capture_output=True, text=True).stdout
        links, cur = [], None
        for line in out.splitlines():
            if line[:1] not in (" ", "\t"):
                cur = line.strip()
            else:
                s = line.strip()
                if cur and cur.startswith(ROUTER + ":monitor") and s.startswith("|->"):
                    links.append((cur, s[3:].strip()))
        return links

    def router_targets(self):
        return {t.rsplit(":", 1)[0] for _, t in self.router_links()}

    def apply_outputs(self, names):
        """Make the router feed exactly `names` (one or several, simultaneously)."""
        self.ensure_router()
        for mport, tgt in self.router_links():                       # clear old links
            subprocess.run(["pw-link", "-d", mport, tgt], capture_output=True)
        for n in names:
            subprocess.run(["pactl", "suspend-sink", n, "0"], capture_output=True)  # wake
            for ch in ("FL", "FR"):
                subprocess.run(["pw-link", f"{ROUTER}:monitor_{ch}", f"{n}:playback_{ch}"],
                               capture_output=True)
        subprocess.run(["pactl", "set-default-sink", ROUTER], capture_output=True)
        subprocess.run(["pactl", "set-sink-mute", ROUTER, "0"], capture_output=True)
        self.save_state(names)

    def load_state(self):
        try:
            with open(STATE) as f:
                want = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            want = []
        have = {s.name for s in self.real_sinks()}
        want = [n for n in want if n in have]
        if not want:
            first = next((s.name for s in self.real_sinks()), None)
            want = [first] if first else []
        return want

    def save_state(self, names):
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE, "w") as f:
                f.write("\n".join(names) + "\n")
        except OSError:
            pass


class Events(threading.Thread):
    """Background libpulse event listener -> schedules UI refresh (no polling)."""

    def __init__(self, on_change):
        super().__init__(daemon=True)
        self.on_change = on_change
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                p = pulsectl.Pulse("timbre-events")
                p.event_mask_set("sink", "sink_input", "server")

                def _h(ev):
                    raise pulsectl.PulseLoopStop

                p.event_callback_set(_h)
                while not self._stop:
                    p.event_listen(timeout=3)         # returns on event OR 3s (safety heartbeat)
                    GLib.idle_add(self.on_change)
            except Exception:
                GLib.idle_add(self.on_change)
                time.sleep(1.5)


class Row:
    """Shared slider/mute row scaffolding."""

    def __init__(self, app):
        self.app = app
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.add_css_class("audio-row")

    def _mk_scale(self, on_change):
        s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        s.set_hexpand(True)
        s.set_draw_value(False)
        s.connect("value-changed", on_change)
        return s

    def _mk_mute(self, on_toggle):
        b = Gtk.ToggleButton(icon_name="audio-volume-high-symbolic")
        b.add_css_class("flat")
        b.connect("toggled", on_toggle)
        return b

    def _sync_mute(self, btn, muted):
        btn.set_active(muted)
        btn.set_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")


class SinkRow(Row):
    def __init__(self, app, sink):
        super().__init__(app)
        self.sink = sink
        self.check = Gtk.CheckButton()
        self.check.set_tooltip_text("Play here (tick several to play on all at once)")
        self.check.set_valign(Gtk.Align.CENTER)
        self.check.connect("toggled", self._on_check)
        self.box.append(self.check)

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        nb.set_size_request(118, -1)
        self.name = Gtk.Label(xalign=0, ellipsize=3)
        self.sub = Gtk.Label(xalign=0, label="")
        self.sub.add_css_class("dim")
        nb.append(self.name)
        nb.append(self.sub)
        self.box.append(nb)

        self.scale = self._mk_scale(self._on_vol)
        self.box.append(self.scale)
        self.pct = Gtk.Label(label="0%")
        self.pct.add_css_class("pct")
        self.box.append(self.pct)
        self.mute = self._mk_mute(self._on_mute)
        self.box.append(self.mute)

    def _on_check(self, _b):
        if not self.app.syncing:
            self.app.schedule_apply_outputs()

    def _on_vol(self, s):
        p = int(s.get_value())
        self.pct.set_text(f"{p}%")
        if self.app.syncing:
            return
        self.app.touch(("sink", self.sink.index))
        try:
            self.app.pa.set_vol(self.sink, p / 100.0)
        except Exception:
            self.app.recover()

    def _on_mute(self, b):
        if not self.app.syncing:
            try:
                self.app.pa.set_mute(self.sink, b.get_active())
            except Exception:
                self.app.recover()

    def update(self, sink, default_name, combine_on, slaves):
        self.sink = sink
        self.app.syncing = True
        try:
            self.name.set_text(sink.description)
            active = (sink.name in slaves) if combine_on else (sink.name == default_name)
            self.check.set_active(active)
            self.sub.set_text("output" if active else "")
            if not self.app.recently_touched(("sink", sink.index)):
                self.scale.set_value(round(self.app.pa.vol(sink) * 100))
            self.pct.set_text(f"{int(self.scale.get_value())}%")
            self._sync_mute(self.mute, bool(sink.mute))
        finally:
            self.app.syncing = False


class AppRow(Row):
    def __init__(self, app, si):
        super().__init__(app)
        self.si = si
        self.icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        self.icon.add_css_class("app-icon")
        self.icon.set_valign(Gtk.Align.CENTER)
        self.box.append(self.icon)

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        nb.set_size_request(110, -1)
        self.name = Gtk.Label(xalign=0, ellipsize=3)
        self.sub = Gtk.Label(xalign=0, label="")
        self.sub.add_css_class("dim")
        nb.append(self.name)
        nb.append(self.sub)
        self.box.append(nb)

        self.scale = self._mk_scale(self._on_vol)
        self.box.append(self.scale)
        self.pct = Gtk.Label(label="0%")
        self.pct.add_css_class("pct")
        self.box.append(self.pct)
        self.mute = self._mk_mute(self._on_mute)
        self.box.append(self.mute)

        self.route = Gtk.MenuButton(icon_name="audio-speakers-symbolic")
        self.route.add_css_class("flat")
        self.route.set_tooltip_text("Send this app to...")
        self.route.set_create_popup_func(self._menu)
        self.box.append(self.route)

    def _menu(self, mb):
        pop = Gtk.Popover()
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4)
        try:
            allsinks = self.app.pa.sinks()
        except Exception:
            allsinks = []
        router = next((s for s in allsinks if s.name == ROUTER), None)
        ee = next((s for s in allsinks if s.name == EE_SINK), None)
        targets = []
        if router:
            targets.append((router.index, "Master (all outputs)"))
        if ee:
            targets.append((ee.index, "Through EasyEffects"))
        for s in allsinks:
            if is_real(s.name):
                targets.append((s.index, s.description))
        for idx, lbl in targets:
            btn = Gtk.Button(label=lbl)
            btn.add_css_class("flat")
            btn.get_child().set_xalign(0)
            btn.connect("clicked", self._pick, idx, pop)
            b.append(btn)
        pop.set_child(b)
        mb.set_popover(pop)

    def _pick(self, _b, idx, pop):
        pop.popdown()
        try:
            self.app.pa.move_app(self.si, idx)
        except Exception:
            self.app.recover()
        self.app.schedule_refresh()

    def _on_vol(self, s):
        p = int(s.get_value())
        self.pct.set_text(f"{p}%")
        if self.app.syncing:
            return
        self.app.touch(("app", self.si.index))
        try:
            self.app.pa.set_vol(self.si, p / 100.0)
        except Exception:
            self.app.recover()

    def _on_mute(self, b):
        if not self.app.syncing:
            try:
                self.app.pa.set_mute(self.si, b.get_active())
            except Exception:
                self.app.recover()

    def update(self, si, sink_objs):
        self.si = si
        self.app.syncing = True
        try:
            self.name.set_text(clean_app_name(si))
            self.icon.set_from_icon_name(app_icon_name(si, self.app.icon_theme))
            dest = sink_objs.get(si.sink)
            self.sub.set_text(dest.description if dest else "")
            self.route.set_tooltip_text("Output: " + (dest.description if dest else "?"))
            if not self.app.recently_touched(("app", si.index)):
                self.scale.set_value(round(self.app.pa.vol(si) * 100))
            self.pct.set_text(f"{int(self.scale.get_value())}%")
            self._sync_mute(self.mute, bool(si.mute))
        finally:
            self.app.syncing = False


class Timbre(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.haku.Timbre")
        self.syncing = False
        self._touch = {}
        self._apply_src = 0
        self._refresh_src = 0
        self.sink_rows = {}
        self.app_rows = {}

    def touch(self, key):
        self._touch[key] = time.monotonic()

    def recently_touched(self, key, window=1.2):
        return (time.monotonic() - self._touch.get(key, 0)) < window

    def recover(self):
        try:
            self.pa.reconnect()
        except Exception:
            pass

    def schedule_refresh(self):
        if self._refresh_src:
            return
        self._refresh_src = GLib.timeout_add(120, self._do_refresh)

    def _do_refresh(self):
        self._refresh_src = 0
        try:
            self.refresh()
        except Exception:
            self.recover()
        return False

    def schedule_apply_outputs(self):
        if self._apply_src:
            GLib.source_remove(self._apply_src)
        self._apply_src = GLib.timeout_add(350, self._apply_outputs)

    def _apply_outputs(self):
        self._apply_src = 0
        names = [r.sink.name for r in self.sink_rows.values()
                 if r.check.get_active() and is_real(r.sink.name)]
        if names:                        # never silence everything by unticking all
            try:
                self.pa.apply_outputs(names)
            except Exception:
                self.recover()
        self.schedule_refresh()
        return False

    def do_activate(self):
        self.pa = PA()
        self.pa.ensure_router()
        self.icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.win = Adw.ApplicationWindow(application=self, title="Timbre",
                                         default_width=480, default_height=680)
        header = Adw.HeaderBar()
        ee = Gtk.Button(icon_name="application-x-addon-symbolic")
        ee.set_tooltip_text("Open EasyEffects (EQ / effects)")
        ee.connect("clicked", lambda *_: self._launch("easyeffects"))
        header.pack_end(ee)
        pavu = Gtk.Button(icon_name="emblem-system-symbolic")
        pavu.set_tooltip_text("Open full mixer (pavucontrol)")
        pavu.connect("clicked", lambda *_: self._launch("pavucontrol"))
        header.pack_end(pavu)

        self.out_group = Adw.PreferencesGroup(title="Output devices")
        self.out_group.set_description("Tick one to switch, or several to play on all at once.")
        self.app_group = Adw.PreferencesGroup(title="Playing")
        self.apps_empty = Adw.ActionRow(title="Nothing is playing")
        self.apps_empty.set_subtitle("Start some audio and it shows up here.")
        self.app_group.add(self.apps_empty)

        page = Adw.PreferencesPage()
        page.add(self.out_group)
        page.add(self.app_group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        self.win.set_content(view)
        self.win.present()

        self.refresh()
        self.events = Events(self.schedule_refresh)
        self.events.start()

    def _launch(self, *cmd):
        try:
            subprocess.Popen(list(cmd), start_new_session=True)
        except Exception:
            pass

    def refresh(self):
        default = self.pa.default_name()
        combine_on = self.pa.combine_active()
        slaves = set(self.pa.combine_slaves()) if combine_on else set()

        sinks = self.pa.real_sinks()
        seen = set()
        for s in sinks:
            seen.add(s.index)
            row = self.sink_rows.get(s.index)
            if row is None:
                row = SinkRow(self, s)
                self.sink_rows[s.index] = row
                self.out_group.add(row.box)
            row.update(s, default, combine_on, slaves)
        for idx in list(self.sink_rows):
            if idx not in seen:
                self.out_group.remove(self.sink_rows[idx].box)
                del self.sink_rows[idx]

        sink_objs = self.pa.name_by_index()
        apps = self.pa.apps()
        seen = set()
        for a in apps:
            seen.add(a.index)
            row = self.app_rows.get(a.index)
            if row is None:
                row = AppRow(self, a)
                self.app_rows[a.index] = row
                self.app_group.add(row.box)
            row.update(a, sink_objs)
        for idx in list(self.app_rows):
            if idx not in seen:
                self.app_group.remove(self.app_rows[idx].box)
                del self.app_rows[idx]

        self.apps_empty.set_visible(len(self.app_rows) == 0)


def main():
    import sys
    if "--apply" in sys.argv:
        # Headless: wire the system output pipeline from saved state (used by the
        # login service so audio works on boot before the GUI is ever opened).
        pa = PA()
        pa.ensure_router()
        pa.apply_outputs(pa.load_state())
        return 0
    return Timbre().run(None)


if __name__ == "__main__":
    main()
