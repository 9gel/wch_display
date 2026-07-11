{ pkgs ? import <nixpkgs> { } }:

let
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    python3
    uv
  ];
}

