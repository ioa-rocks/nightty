# SPDX-License-Identifier: MPL-2.0
"""Editable Nightty palette and font sizes."""

import json
from pathlib import Path
import re
from string import Template


DEFAULT_FILE = Path(__file__).resolve().parent.parent / "appearance.json"
PALETTE_KEYS = {
    "background", "surface", "header", "header_text", "foreground", "muted",
    "subtle", "border", "accent", "accent_text", "directory", "unknown",
    "image", "video", "audio", "document", "source", "data", "archive",
    "symlink", "executable",
}
OPTIONAL_PALETTE_KEYS = {"terminal_foreground", "terminal_background", "marked"}
FONT_SIZES = {"ui_px", "files_px", "small_px", "heading_px"}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
FONT_FAMILY = re.compile(r"[a-zA-Z0-9 _-]{1,64}\Z")

CSS = Template("""
window { background:$background; color:$foreground; font-size:${ui_px}px; }
headerbar, headerbar:backdrop { background:$header; background-image:none; color:$header_text; border-bottom:1px solid $border; box-shadow:none; }
headerbar button { background:transparent; color:$header_text; border:0; border-radius:0; padding:4px 9px; box-shadow:none; }
headerbar button.suggested-action { background:$accent; color:$header_text; }
headerbar button.suggested-action label { color:$header_text; }
headerbar button.top-tab { padding:5px 11px; border-bottom:2px solid transparent; }
headerbar button.top-tab-active { background:$surface; color:$foreground; border-bottom-color:$accent; }
headerbar button.tab-close { color:$muted; padding:5px 5px; }
headerbar button.tab-close:hover { color:$header_text; }
button { border-radius:2px; padding:4px 8px; }
button:checked { background:$accent; color:$accent_text; }
dialog.mc-dialog { background:$surface; color:$foreground; border:2px solid $accent; border-radius:0; box-shadow:none; font-family:'$family'; font-size:${ui_px}px; }
.mc-dialog-title { background:$surface; color:$accent; border-bottom:1px solid $border; padding:5px 14px; font-size:${small_px}px; font-weight:700; }
.mc-dialog-message { color:$foreground; padding:18px 20px 8px; }
.mc-dialog-detail { color:$muted; font-size:${small_px}px; padding:0 20px 10px; }
dialog.mc-dialog button.mc-dialog-button { background:$background; background-image:none; color:$foreground; border:1px solid $border; border-radius:0; box-shadow:none; min-width:84px; padding:4px 12px; font-family:'$family'; font-size:${ui_px}px; }
dialog.mc-dialog button.mc-dialog-default, dialog.mc-dialog button.mc-dialog-button:focus { background:$accent; color:$accent_text; border-color:$accent; }
dialog.mc-dialog button.mc-dialog-button:hover { border-color:$foreground; }
entry { background:$surface; color:$foreground; border:1px solid $border; border-radius:0; font-family:'$family'; font-size:${files_px}px; }
treeview { background:$background; color:$foreground; font-family:'$family'; font-size:${files_px}px; outline-width:0; }
treeview:selected { background:$accent; color:$accent_text; outline-width:0; }
.pane-inactive treeview:selected { background:$surface; color:$subtle; outline-width:0; }
iconview { background:$background; color:$foreground; font-family:'$family'; font-size:${files_px}px; font-weight:${files_weight}; outline-width:0; }
iconview:selected { background:$accent; color:$accent_text; outline-width:0; }
.pane-inactive iconview:selected { background:$surface; color:$subtle; outline-width:0; }
treeview header button { background:$surface; color:$muted; border:0; border-radius:0; font-size:${small_px}px; }
notebook header { background:$surface; }
textview, textview text { background:$surface; color:$foreground; font-size:${ui_px}px; }
.muted { color:$muted; font-size:${small_px}px; }
.heading { font-size:${heading_px}px; font-weight:600; color:$foreground; }
.section { font-size:${small_px}px; font-weight:600; color:$muted; }
.pane-list-rule { background:$border; min-height:2px; }
.footer-box { background:$surface; border:1px solid $border; padding:3px 8px; }
.footer-details { color:$foreground; font-family:'$family'; font-size:${small_px}px; }
.footer-marked { color:$marked; font-family:'$family'; font-size:${small_px}px; font-weight:700; }
.footer-folder { color:$muted; font-family:'$family'; font-size:${small_px}px; }
.footer-folder-stats { color:$subtle; font-family:'$family'; font-size:${small_px}px; }
.command-row { background:$surface; padding:3px 6px; }
.command-prompt { color:$accent; font-family:'$family'; font-size:${files_px}px; }
entry.command-entry { background:transparent; border:0; box-shadow:none; padding:2px; }
.pane-active { border-top:2px solid $accent; }
.pane-inactive { border-top:2px solid $surface; }
.inspector-details { background:$surface; border-left:1px solid $border; padding:18px; }
.gallery-tile { background:$surface; border:1px solid $border; border-radius:3px; padding:8px; }
flowboxchild { border:2px solid transparent; }
flowboxchild:selected { background:$accent; border-color:$accent; }
.gallery-name { font-family:'$family'; font-size:${small_px}px; font-weight:${files_weight}; }
.gallery-name.gallery-folder-name { font-size:${ui_px}px; }
.gallery-parent { color:$directory; font-family:'$family'; font-size:64px; font-weight:900; }
.gallery-excerpt { color:$muted; font-family:'$family'; font-size:${small_px}px; }
.inspector-details .muted { font-size:${small_px}px; }
""")


def load_appearance(path=None):
    source = Path(path) if path else DEFAULT_FILE
    value = json.loads(source.read_text())
    if not isinstance(value, dict) or not {"palette", "fonts", "ansi"} <= value.keys():
        raise ValueError(f"Missing appearance sections in {source}")
    palette, fonts, ansi = value["palette"], value["fonts"], value["ansi"]
    if not isinstance(palette, dict) or not PALETTE_KEYS <= palette.keys():
        raise ValueError(f"Incomplete palette in {source}")
    if any(not isinstance(palette[key], str) or not HEX_COLOR.fullmatch(palette[key])
           for key in PALETTE_KEYS | (OPTIONAL_PALETTE_KEYS & palette.keys())):
        raise ValueError(f"Palette colors must be #RRGGBB values in {source}")
    if not isinstance(fonts, dict) or not FONT_SIZES <= fonts.keys():
        raise ValueError(f"Incomplete font sizes in {source}")
    if any(type(fonts[key]) is not int or not 9 <= fonts[key] <= 40 for key in FONT_SIZES):
        raise ValueError(f"Font sizes must be whole numbers from 9 to 40 in {source}")
    fonts.setdefault("files_weight", 700)
    if type(fonts["files_weight"]) is not int or fonts["files_weight"] not in range(100, 901, 100):
        raise ValueError(f"File font weight must be a multiple of 100 from 100 to 900 in {source}")
    if not isinstance(fonts.get("family"), str) or not FONT_FAMILY.fullmatch(fonts["family"]):
        raise ValueError(f"Invalid font family in {source}")
    if not isinstance(ansi, list) or len(ansi) != 16 or any(
        not isinstance(color, str) or not HEX_COLOR.fullmatch(color) for color in ansi
    ):
        raise ValueError(f"ANSI palette must have 16 #RRGGBB colors in {source}")
    return value


def stylesheet(appearance):
    return CSS.substitute({"marked": "#b58900"} | appearance["palette"] | appearance["fonts"]).encode()
