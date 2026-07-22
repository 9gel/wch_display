{ lib, python3Packages }:

let
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf path;
      in !(lib.elem base [ "captures" ".venv" "__pycache__" ".direnv" "data" ".git" ]);
  };
in
python3Packages.buildPythonApplication {
  pname = "csm-panel";
  version = "0.1.0";
  pyproject = true;
  inherit src;

  build-system = [ python3Packages.hatchling ];
  # pyserial: the serial driver. pillow: from-scratch theme compile (render_text)
  # renders StaticText/Number/DateTime glyph masks + converts images. Liberation
  # Sans (the editor's "Arial") is discovered from the Nix store at compile time,
  # so a consumer that runs `ui-compile --render-text` must put a liberation font
  # package on the build/runtime closure (see the theme derivation in the README).
  dependencies = [ python3Packages.pyserial python3Packages.pillow ];

  pythonImportsCheck = [ "csm_panel" "csm_panel.theme" ];

  meta = {
    description = "Driver, theme toolkit and 0x66 streaming service for the CSM050H800480 (1a86:8040) USB panel";
    mainProgram = "csm-panel";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
