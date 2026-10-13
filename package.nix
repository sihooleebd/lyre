{ lib
, python3Packages
, pulseaudio
, pipewire
}:

python3Packages.buildPythonApplication {
  pname = "lyre";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];

  dependencies = [ python3Packages.pulsectl ];

  # The engine shells out to: pactl (null-sink / move-sink-input / default-sink),
  # pw-cli (hosting the filter-chain EQ modules + live band params), and pw-link
  # (wiring the graph). Bake them in -- none are on the user-service PATH.
  makeWrapperArgs = [ "--prefix" "PATH" ":" (lib.makeBinPath [ pulseaudio pipewire ]) ];

  doCheck = false;
  pythonImportsCheck = [ "lyre" ];

  meta = {
    description = "Headless per-app / per-output EQ + routing engine for PipeWire (replaces EasyEffects)";
    homepage = "https://github.com/sihooleebd/lyre";
    license = lib.licenses.mit;
    mainProgram = "lyre";
    platforms = lib.platforms.linux;
  };
}
