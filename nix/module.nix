# Generic NixOS module for the csm-panel dashboard service.
#
#   services.csm-panel = {
#     enable = true;
#     settings = { panel.port = "/dev/ttyACM0"; host = [ { name = "box"; source = "local"; } ]; };
#     # secrets (e.g. a Beszel password) come from an env file, never the store:
#     environmentFile = "-/run/csm-panel/env";   # CSM_PANEL_BESZEL_PASSWORD=...
#   };
{ config, lib, pkgs, csmPanelPackage ? null, ... }:
let
  cfg = config.services.csm-panel;
  tomlFormat = pkgs.formats.toml { };
  configFile =
    if cfg.configFile != null then cfg.configFile
    else tomlFormat.generate "csm-panel.toml" cfg.settings;
in
{
  options.services.csm-panel = {
    enable = lib.mkEnableOption "the CSM050H800480 USB panel dashboard";

    package = lib.mkOption {
      type = lib.types.package;
      default = if csmPanelPackage != null then csmPanelPackage
                else pkgs.callPackage ./package.nix { };
      description = "The csm-panel package to run.";
    };

    settings = lib.mkOption {
      type = tomlFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          panel = { port = "/dev/ttyACM0"; interval = 2.0; columns = 2; };
          host = [ { name = "box"; source = "local"; metrics = [ "temps" "cpu" "ram" ]; } ];
        }
      '';
      description = "Contents of config.toml (rendered from this attrset).";
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Use this config.toml verbatim instead of `settings`.";
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "User to run the service as (null = root). Must reach the panel serial device.";
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Group to run the service as.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "-/run/csm-panel/env";
      description = ''
        systemd EnvironmentFile for secrets kept out of the store, e.g.
        `CSM_PANEL_BESZEL_PASSWORD=...`. Prefix with `-` to make it optional.
      '';
    };

    extraServiceConfig = lib.mkOption {
      type = lib.types.attrs;
      default = { };
      description = "Extra serviceConfig merged into the systemd unit.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.csm-panel = {
      description = "CSM050H800480 USB panel dashboard";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      # USB device may be absent/unplugged; keep retrying without a start limit.
      startLimitIntervalSec = 0;
      serviceConfig = {
        Type = "simple";
        ExecStart = "${lib.getExe cfg.package} -c ${configFile} run";
        Restart = "always";
        RestartSec = 3;
        Environment = [ "PYTHONUNBUFFERED=1" ];
      }
      // lib.optionalAttrs (cfg.user != null) { User = cfg.user; }
      // lib.optionalAttrs (cfg.group != null) { Group = cfg.group; }
      // lib.optionalAttrs (cfg.environmentFile != null) { EnvironmentFile = cfg.environmentFile; }
      // cfg.extraServiceConfig;
    };
  };
}
