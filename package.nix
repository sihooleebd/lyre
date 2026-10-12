{ lib
, python3Packages
, wrapGAppsHook4
, gobject-introspection
, gtk4
, libadwaita
, pulseaudio
, pipewire
}:

python3Packages.buildPythonApplication {
  pname = "timbre";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];

  nativeBuildInputs = [
    wrapGAppsHook4
    gobject-introspection
  ];

  buildInputs = [
    gtk4
    libadwaita
  ];

  dependencies = [
    python3Packages.pygobject3
    python3Packages.pulsectl
  ];

  # Routing shells out to `pactl` (pulseaudio) + `pw-link` (pipewire); bake both in
  # since neither is on the user's PATH. Hold off GApps auto-wrap so those tools +
  # the GI/typelib env all land on one wrapper.
  # GSK_RENDERER=cairo: skip GTK4's GL/Vulkan context init at first window (a ~1s cold-start
  # cost on Intel integrated graphics) -- this is a simple control panel, cairo renders it
  # fine and Hyprland does the blur/opacity anyway. --set-default so it stays overridable.
  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=("''${gappsWrapperArgs[@]}" --prefix PATH : ${lib.makeBinPath [ pulseaudio pipewire ]} --set-default GSK_RENDERER cairo)
  '';

  doCheck = false;
  pythonImportsCheck = [ "timbre" ];

  # Launcher entry (so it shows in rofi / the app launcher).
  postInstall = ''
    install -Dm644 timbre.desktop $out/share/applications/dev.haku.Timbre.desktop
  '';

  meta = {
    description = "SoundSource-style audio control panel for PipeWire";
    homepage = "https://github.com/sihooleebd/timbre";
    license = lib.licenses.mit;
    mainProgram = "timbre";
    platforms = lib.platforms.linux;
  };
}
