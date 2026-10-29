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
OPTIONAL_PALETTE_KEYS = {"terminal_foreground", "terminal_background", "marked", "focus",
                         "dialog_background"}
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
headerbar button.transfer-alert { background:#a83f32; color:#ffffff; font-weight:700; }
button { border-radius:2px; padding:4px 8px; }
button:checked { background:$accent; color:$accent_text; }
button.pane-header-control { background:transparent; background-image:none; color:$muted; border:0; box-shadow:none; border-radius:4px; min-width:32px; min-height:32px; padding:3px; }
.pane-toolbar > button.pane-header-control { min-width:28px; padding:2px; }
button.pane-header-control:hover,
button.pane-header-control:focus { background:$background; color:$foreground; }
.pane-toolbar { background:$surface; }
button.pane-address-control { min-width:28px; min-height:28px; margin:2px; }
entry.pane-address-entry { background:$surface; background-image:none; border:0; border-radius:0; box-shadow:none; outline:0; padding-left:8px; padding-right:36px; }
entry.pane-address-entry:focus,
entry.pane-address-entry:focus-within { background-image:linear-gradient(to top, $focus 0px, $focus 2px, transparent 2px); }
.pane-menu-item { min-width:260px; padding:6px 12px; }
window.mc-dialog { background:$dialog_background; color:$foreground; border:3px solid $accent; border-radius:0; box-shadow:none; font-family:'$family'; }
.mc-dialog-content { background:$dialog_background; }
.mc-dialog-title { background:$accent; color:$accent_text; border-bottom:2px solid $border; padding:12px 26px; font-size:${dialog_title_px}px; font-weight:700; }
.mc-dialog-message { color:$foreground; padding:24px 26px 12px; font-size:${dialog_message_px}px; }
.mc-dialog-detail { color:$muted; padding:0 26px 20px; font-size:${dialog_detail_px}px; }
.mc-dialog-actions { padding:14px 26px 24px; border-top:1px solid $accent; }
.mc-conflict-actions { padding:0 26px 12px; }
.mc-conflict-actions:last-child { padding-bottom:24px; }
window.mc-dialog button.mc-dialog-button { background:$surface; background-image:none; color:$foreground; border:2px solid $border; border-radius:2px; box-shadow:none; min-width:150px; min-height:52px; padding:8px 16px; font-family:'$family'; font-size:${dialog_button_px}px; font-weight:700; }
window.mc-dialog button.mc-dialog-button:hover { border-color:$foreground; }
window.mc-dialog button.mc-dialog-button.mc-dialog-focused,
window.mc-dialog button.mc-dialog-button:focus { background:$accent; color:$accent_text; border-color:$accent_text; }
window.mc-dialog.mc-danger-dialog { border-color:#d94e44; }
window.mc-danger-dialog .mc-dialog-title { background:#a83f32; color:#ffffff; border-bottom-color:#d94e44; }
window.mc-danger-dialog .mc-dialog-message { color:#ffbbb3; }
window.mc-danger-dialog button.mc-danger-default { color:#ffbbb3; border-color:#e36b58; }
window.mc-danger-dialog button.mc-danger-default.mc-dialog-focused,
window.mc-danger-dialog button.mc-danger-default:focus { background:#a83f32; color:#ffffff; border-color:#ffc2bb; }
.mc-transfer-fields { padding:20px 26px 12px; }
.mc-transfer-caption { color:$foreground; font-size:${dialog_detail_px}px; font-weight:700; }
.mc-transfer-value { color:$foreground; font-size:${dialog_message_px}px; padding-bottom:12px; }
entry.mc-transfer-entry { min-height:40px; font-size:${dialog_message_px}px; padding:4px 8px; }
dropdown.mc-find-scope { min-height:42px; font-size:${dialog_message_px}px; }
.mc-transfer-error { color:#e36b58; font-size:${dialog_detail_px}px; padding:0 26px 16px; }
.mc-transfer-progress { margin:4px 26px 20px; min-height:12px; }
.search-results-bar { background:$surface; border-bottom:1px solid $border; }
.search-results-label { color:$accent; font-size:${small_px}px; font-weight:700; }
entry { background:$surface; color:$foreground; border:1px solid $border; border-radius:0; font-family:'$family'; font-size:${files_px}px; }
treeview { background:$background; color:$foreground; font-family:'$family'; font-size:${files_px}px; outline-width:0; }
treeview:selected { background:$accent; color:$accent_text; outline-width:0; }
.pane-inactive treeview:selected { background:$surface; color:$subtle; outline-width:0; }
iconview { background:$background; color:$foreground; font-family:'$family'; font-size:${file_text_px}px; font-weight:${files_weight}; outline-width:0; }
iconview:selected { background:$accent; color:$accent_text; outline-width:0; }
.pane-inactive iconview:selected { background:$surface; color:$subtle; outline-width:0; }
treeview header button { background:$surface; color:$muted; border:0; border-radius:0; font-size:${small_px}px; }
notebook header { background:$surface; }
textview, textview text { background:$surface; color:$foreground; font-size:${ui_px}px; }
.muted { color:$muted; font-size:${small_px}px; }
.heading { font-size:${heading_px}px; font-weight:600; color:$foreground; }
.section { font-size:${small_px}px; font-weight:600; color:$muted; }
.pane-list-rule { background:$border; min-height:2px; }
.pane-content-left { border-left:2px solid $border; }
.pane-content-right { border-right:2px solid $border; }
.footer-box { background:$surface; border:1px solid $border; padding:3px 8px; }
.footer-box.pane-footer-left, .footer-box.pane-footer-right { border-top-width:0; }
.footer-box.pane-footer-left { border-left-width:2px; border-right-width:0; }
.footer-box.pane-footer-right { border-left-width:0; border-right-width:2px; }
.footer-details { color:$foreground; font-family:'$family'; font-size:${small_px}px; }
.footer-marked { color:$marked; font-family:'$family'; font-size:${small_px}px; font-weight:700; }
.footer-folder { color:$muted; font-family:'$family'; font-size:${small_px}px; }
.footer-folder-stats { color:$subtle; font-family:'$family'; font-size:${small_px}px; }
.command-row { background:$surface; padding:3px 6px; }
.command-prompt { color:$accent; font-family:'$family'; font-size:${files_px}px; }
entry.command-entry { background:$surface; background-image:none; border:0; border-radius:0; box-shadow:none; outline:0; padding:2px; }
entry.command-entry:focus,
entry.command-entry:focus-within { background-image:linear-gradient(to top, $focus 0px, $focus 2px, transparent 2px); }
.pane-active { border-top:2px solid $accent; }
.pane-inactive { border-top:2px solid $surface; }
.file-split > separator { min-width:2px; background:$background; border-left:2px solid $border; border-right:2px solid $border; }
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
    fonts = appearance["fonts"]
    # At the default 26px, GTK loses one scanline from capital top strokes.
    # A quarter pixel restores it without increasing the file row's height.
    file_text_px = fonts["files_px"] + 0.25
    dialog_sizes = {"dialog_title_px": max(fonts["heading_px"], fonts["ui_px"] + 6),
                    "dialog_message_px": fonts["ui_px"] + 4,
                    "dialog_detail_px": fonts["ui_px"] + 2,
                    "dialog_button_px": fonts["ui_px"] + 4}
    palette = {"marked": "#b58900", "focus": "#cb4b16",
               "dialog_background": appearance["palette"].get("dialog_background", appearance["palette"]["background"])}
    return CSS.substitute(palette | appearance["palette"] | fonts | dialog_sizes |
                          {"file_text_px": file_text_px}).encode()
