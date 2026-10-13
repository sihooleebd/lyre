{
  description = "bonfire -- headless per-app / per-output EQ + routing engine for PipeWire";

  # Pinned to plain nixos-unstable (same track as the host's nixpkgs-upstream) so the
  # whole closure substitutes from cache.nixos.org -- the consuming system flake adds
  # this input WITHOUT nixpkgs.follows, so nothing here is rebuilt from source.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        bonfire = pkgs.callPackage ./package.nix { };
        default = bonfire;
      });

      apps = forAllSystems (pkgs: rec {
        bonfire = {
          type = "app";
          program = "${self.packages.${pkgs.system}.bonfire}/bin/bonfire";
          meta.description = "Headless PipeWire EQ + routing engine";
        };
        default = bonfire;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pulsectl ]))
            pkgs.pulseaudio
            pkgs.pipewire
          ];
          shellHook = ''echo "bonfire dev shell -- run: python -m bonfire.main daemon"'';
        };
      });
    };
}
