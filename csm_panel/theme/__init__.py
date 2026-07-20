"""Theme toolkit for the CSM050H800480 panel.

Two pieces, both reverse-engineered from the vendor Windows editor (see
``PROTOCOL_NOTES.md``):

* :mod:`csm_panel.theme.ui_codec` — decode/encode the editor's RC4-encrypted
  ``.ui`` project files (XML).
* :mod:`csm_panel.theme.compiler` — compile a ``.ui`` into the binary theme
  *blob* the panel consumes (the thing you flash with ``Panel.send_theme``).

Workflow: design a theme in the vendor editor (or hand-write the XML), then
``ui_codec.decode`` it, ``compiler.compile_ui_to_blob`` it against a known-good
base blob (for the pre-rendered glyph/text resources), and flash the result —
all on Linux, no Windows needed.
"""
from .ui_codec import decode, encode, RC4_KEY, rc4
from .compiler import compile_ui_to_blob, parse_ui

__all__ = ["decode", "encode", "rc4", "RC4_KEY", "compile_ui_to_blob", "parse_ui"]
