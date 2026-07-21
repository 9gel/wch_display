{ pkgs ? import <nixpkgs> { } }:

let
in
pkgs.mkShell {
  buildInputs = with pkgs; [
    python3
    uv
    ffmpeg        # frame extraction for tools/video_to_frames.py
    imagemagick   # `magick` — panel-safe JPEG encoding (libjpeg/JFIF baseline)
  ];
}

