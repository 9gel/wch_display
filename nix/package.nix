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
  dependencies = [ python3Packages.pyserial ];

  pythonImportsCheck = [ "csm_panel" "csm_panel.theme" ];

  meta = {
    description = "Driver, theme toolkit and 0x66 streaming service for the CSM050H800480 (1a86:8040) USB panel";
    mainProgram = "csm-panel";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
