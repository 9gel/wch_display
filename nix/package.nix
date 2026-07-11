{ lib, python3Packages, makeWrapper, dejavu_fonts }:

# The dashboard uses DejaVu at runtime; wire the exact font files via env so no
# fontconfig setup is needed inside the service sandbox.
let
  fontBold = "${dejavu_fonts}/share/fonts/truetype/DejaVuSans-Bold.ttf";
  fontReg = "${dejavu_fonts}/share/fonts/truetype/DejaVuSans.ttf";
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf path;
      in !(lib.elem base [ "captures" ".venv" "__pycache__" ".direnv" "docs" ".git" ]);
  };
in
python3Packages.buildPythonApplication {
  pname = "csm-panel";
  version = "0.1.0";
  pyproject = true;
  inherit src;

  build-system = [ python3Packages.hatchling ];
  nativeBuildInputs = [ makeWrapper ];
  dependencies = with python3Packages; [ pillow pyserial ];

  postInstall = ''
    wrapProgram $out/bin/csm-panel \
      --set-default CSM_PANEL_FONT_BOLD ${fontBold} \
      --set-default CSM_PANEL_FONT ${fontReg}
  '';

  pythonImportsCheck = [ "csm_panel" ];

  meta = {
    description = "Driver and host-metrics dashboard for the CSM050H800480 (1a86:8040) USB panel";
    mainProgram = "csm-panel";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
