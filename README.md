# lyre

**One native audio engine for Linux** — per-app and per-output EQ **plus** routing and
multi-device output for PipeWire, with no EasyEffects and no GUI app. It's a headless
daemon you drive over a tiny CLI (a desktop panel is the intended front-end).

Everything flows through one two-stage graph:

```
app A ─▶ [EQ·A] ─┐
app B ─▶ [EQ·B] ─┼▶ 🔥 lyre ─▶ monitor ─┬▶ [EQ·speakers] ─▶ speakers
app C ─▶ [EQ·C] ─┘   (null-sink: merge)     └▶ [EQ·headset]  ─▶ headset
```

- **Per-app EQ** — each app gets its own 10-band EQ, applied before it reaches the mix.
- **The lyre** — a single null-sink everything merges into (the system default output),
  then fanned out to one or several devices at once via plain graph links.
- **Per-output EQ** — each output device gets its own 10-band EQ on the way out.
- **Routing + multi-device** — send any app anywhere; play on several devices simultaneously.

Every EQ box is a native PipeWire `module-filter-chain` (builtin `bq_peaking`/shelf biquad
bands) that lyre hosts and tweaks live — **no EasyEffects**. Every arrow is a `pw-link`;
the merge/fan-out can't be scrambled by `module-stream-restore` the way `module-combine-sink`
could, so simultaneous output stays working.

## How it works

lyre is a daemon (a filter-chain only lives as long as the client that loaded it, so the
daemon *hosts* every chain). It reacts to apps and devices appearing via a libpulse event
loop, keeps the graph correct, and exposes a unix-socket CLI:

```sh
lyre daemon                 # the engine (run from a systemd --user service)
lyre outputs speakers hdmi  # fan the lyre out to these device(s)
lyre eq app Firefox 3 -4    # per-app EQ: band 3 to -4 dB
lyre eq out speakers 0 6    # per-output EQ: band 0 to +6 dB
lyre eq app Firefox reset   # flatten
lyre status                 # JSON: devices, apps, bands, current EQ/outputs
```

Bands are the 10-band ISO set (31 Hz … 16 kHz). State persists in
`~/.config/lyre/state.json`.

## Install

```nix
{
  inputs.lyre.url = "github:sihooleebd/lyre";

  # NixOS / home-manager: install the package and run the daemon at login
  environment.systemPackages = [ inputs.lyre.packages.${pkgs.system}.default ];

  systemd.user.services.lyre = {
    Unit = {
      Description = "lyre -- PipeWire EQ + routing engine";
      After = [ "pipewire.service" "pipewire-pulse.service" "wireplumber.service" ];
      PartOf = [ "graphical-session.target" ];
    };
    Install.WantedBy = [ "graphical-session.target" ];
    Service = {
      ExecStart = "${inputs.lyre.packages.${pkgs.system}.default}/bin/lyre daemon";
      Restart = "on-failure"; RestartSec = 2;
    };
  };
}
```

If the daemon ever dies, WirePlumber still picks a real device as the default — you're never
left without sound.

## Development

```sh
nix develop
python -m lyre.main daemon        # in one shell
python -m lyre.main status        # in another
```

It's all in `lyre/main.py` — the EQ-chain hosting, the two-stage graph, the engine, and
the CLI. `package.nix` is the derivation (`buildPythonApplication` with `pactl` / `pw-cli` /
`pw-link` on its `PATH`); `flake.nix` exposes `packages`, `apps`, and a `devShell`.

## License

[MIT](LICENSE) © Benjamin Lee
