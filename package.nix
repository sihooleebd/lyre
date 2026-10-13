{ lib
, python3Packages
, pulseaudio
, pipewire
, libmysofa
}:

python3Packages.buildPythonApplication {
  pname = "bonfire";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = [ python3Packages.pulsectl ];

  # The engine shells out to: pactl (null-sink / move-sink-input / default-sink),
  # pw-cli (hosting the filter-chain EQ modules + live band params), and pw-link
  # (wiring the graph). Bake them in -- none are on the user-service PATH.
  # BONFIRE_SOFA = the HRTF dataset the per-app spatializer loads (libmysofa ships
  # MIT KEMAR); pinned here so spatial mode doesn't depend on a store-path glob.
  makeWrapperArgs = [
    "--prefix" "PATH" ":" (lib.makeBinPath [ pulseaudio pipewire ])
    "--set-default" "BONFIRE_SOFA" "${libmysofa}/share/libmysofa/MIT_KEMAR_normal_pinna.sofa"
  ];

  doCheck = false;
  pythonImportsCheck = [ "bonfire" ];

  meta = {
    description = "Headless per-app / per-output EQ + routing engine for PipeWire (replaces EasyEffects)";
    homepage = "https://github.com/sihooleebd/bonfire";
    license = lib.licenses.mit;
    mainProgram = "bonfire";
    platforms = lib.platforms.linux;
  };
}
