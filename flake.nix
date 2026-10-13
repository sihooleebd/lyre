{
  description = "lyre -- headless per-app / per-output EQ + routing engine for PipeWire";

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
        lyre = pkgs.callPackage ./package.nix { };
        default = lyre;
      });

      apps = forAllSystems (pkgs: rec {
        lyre = {
          type = "app";
          program = "${self.packages.${pkgs.system}.lyre}/bin/lyre";
          meta.description = "Headless PipeWire EQ + routing engine";
        };
        default = lyre;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pulsectl ]))
            pkgs.pulseaudio
            pkgs.pipewire
          ];
          shellHook = ''echo "lyre dev shell -- run: python -m lyre.main daemon"'';
        };
      });
    };
}
