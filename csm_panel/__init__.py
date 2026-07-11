"""Driver + toolkit for the CSM050H800480 (1a86:8040) USB panel.

The panel is a JPEG framebuffer: you upload a "theme package" containing one or
more 800x480 JPEGs. See PROTOCOL_NOTES.md for the wire format.
"""
from .panel import Panel, PANEL_W, PANEL_H

__all__ = ["Panel", "PANEL_W", "PANEL_H"]
