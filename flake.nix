{
  description = "Timbre -- SoundSource-style audio control panel for PipeWire";

  # Pinned to plain nixos-unstable (same track as the host's nixpkgs-upstream) so the
  # whole closure substitutes from cache.nixos.org -- the consuming system flake adds
  # this as an input WITHOUT nixpkgs.follows, so nothing here is rebuilt from source.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        timbre = pkgs.callPackage ./package.nix { };
        default = timbre;
      });

      apps = forAllSystems (pkgs: rec {
        timbre = {
          type = "app";
          program = "${self.packages.${pkgs.system}.timbre}/bin/timbre";
          meta.description = "SoundSource-style audio control panel for PipeWire";
        };
        default = timbre;
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.pygobject3 ps.pulsectl ]))
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.gobject-introspection
            pkgs.pulseaudio
          ];
          shellHook = ''
            echo "timbre dev shell -- run: python -m timbre.main"
          '';
        };
      });
    };
}
