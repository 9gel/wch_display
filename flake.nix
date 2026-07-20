{
  description = "Driver, theme toolkit and 0x66 streaming service for the CSM050H800480 (1a86:8040) USB panel";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        csm-panel = pkgs.callPackage ./nix/package.nix { };
        default = csm-panel;
      });

      overlays.default = final: _prev: {
        csm-panel = final.callPackage ./nix/package.nix { };
      };

      # NixOS module. Defaults `services.csm-panel.package` to this flake's build.
      nixosModules.default = { pkgs, ... } @ args:
        import ./nix/module.nix (args // {
          csmPanelPackage = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.python3 ];
        };
      });
    };
}
