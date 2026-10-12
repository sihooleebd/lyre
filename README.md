# Timbre

**Universal audio routing for Linux** — a [SoundSource](https://rogueamoeba.com/soundsource/) /
FineTune-style control panel for PipeWire, in GTK4 + libadwaita.

One small window to control all your system audio:

- **Output devices** — a volume slider per device (per-output gain), mute, and which
  device(s) you're playing on. Tick **one** to switch output, or **several** to play
  on all of them at once.
- **Per-app routing** — send any individual app to any specific device (or through
  EasyEffects), independent of everything else.
- **Per-app volume & mute**, with real app names and icons.

It's **event-driven** (updates the instant anything changes — no polling), and it talks
to PipeWire through `pulsectl` (libpulse) and `pw-link`.

## Why

PulseAudio-style `module-combine-sink` is fragile on PipeWire: its internal streams get
re-pinned by `module-stream-restore`, which can route one back into an effects sink and
create a feedback loop that kills playback. Timbre instead builds a single **null-sink
"router"** and fans it out to your devices with plain graph links — links can't be
scrambled, so simultaneous output to several devices just works and stays working.

```
apps ─▶ Timbre router (a null-sink, the default output)
            └─▶ pw-link fan-out ─▶ the device(s) you pick
```

Picking outputs = choosing which devices the router's monitor is linked to. One device or
many is the exact same operation.

## Install / run

```sh
nix run github:sihooleebd/timbre
```

### As a flake input

```nix
{
  inputs.timbre.url = "github:sihooleebd/timbre";

  # in a NixOS / home-manager module:
  environment.systemPackages = [ inputs.timbre.packages.${pkgs.system}.default ];
}
```

### Wire it as the system output pipeline (optional)

`timbre --apply` sets up the whole pipeline headlessly from `~/.config/timbre/outputs`
(creates the router, links it to the saved devices, makes it the default). Run it from a
systemd **user** service at login so audio is routed before the GUI is ever opened:

```nix
systemd.user.services.timbre-pipeline = {
  Unit = {
    Description = "Timbre system output pipeline";
    After = [ "pipewire.service" "pipewire-pulse.service" "wireplumber.service" ];
    PartOf = [ "graphical-session.target" ];
  };
  Install.WantedBy = [ "graphical-session.target" ];
  Service = { Type = "oneshot"; RemainAfterExit = true;
              ExecStart = "${pkgs.timbre}/bin/timbre --apply"; };
};
```

If the service ever fails, WirePlumber still picks a real device as the default, so you're
never left without sound.

## EasyEffects

If you use [EasyEffects](https://github.com/wwmm/easyeffects) for EQ/effects, it sits in
front of the router (apps → EasyEffects → router → devices); the per-app menu can send
individual apps *through EasyEffects* or straight to a device.

## Development

```sh
nix develop
python -m timbre.main
```

Everything lives in `timbre/main.py` — the PipeWire wrapper and the GTK UI. `package.nix`
is the derivation (`buildPythonApplication`, GTK-wrapped, with `pactl` + `pw-link` on its
`PATH`); `flake.nix` exposes `packages`, `apps`, and a `devShell`.

## License

[MIT](LICENSE) © Benjamin Lee
