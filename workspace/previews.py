# SPDX-License-Identifier: MPL-2.0
"""Thumbnail and inspector renderers for files that need an external decoder."""

from datetime import datetime
import os
import stat
import subprocess

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib


def format_size(value):
    """Human-readable byte count with binary units."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024



def pdf_pixbuf(path, edge):
    """Render the first PDF page at thumbnail or inspector size."""
    result = subprocess.run(
        ["pdftocairo", "-f", "1", "-l", "1", "-singlefile", "-scale-to", str(edge),
         "-png", str(path), "-"],
        capture_output=True, check=True, timeout=10,
    )
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(result.stdout)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ValueError("PDF has no renderable first page")
    return pixbuf


def read_preview(path, width, height):
    """Inspector content for a path: ('image', pixbuf, details) or ('text', text, details)."""
    info = path.stat()
    content_type = Gio.content_type_guess(str(path), None)[0] or "application/octet-stream"
    directory = path.is_dir()
    description = "Directory" if directory else Gio.content_type_get_description(content_type) or content_type
    details = ["Name", path.name, "", "Type", description, content_type,
               "", "Size", "—" if directory else f"{format_size(info.st_size)} ({info.st_size:,} bytes)",
               "", "Modified", datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
               "", "Permissions", stat.filemode(info.st_mode),
               "", "Owner", f"{info.st_uid}:{info.st_gid}",
               "", "Location", str(path.parent)]
    try:
        file_info = Gio.File.new_for_path(str(path)).query_info(
            Gio.FILE_ATTRIBUTE_TIME_CREATED, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
        if file_info.has_attribute(Gio.FILE_ATTRIBUTE_TIME_CREATED):
            created = file_info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_CREATED)
            details += ["", "Created", datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")]
    except GLib.Error:
        pass
    try:
        filesystem = os.statvfs(path)
        details += ["", "Available on filesystem",
                    format_size(filesystem.f_bavail * filesystem.f_frsize)]
    except OSError:
        pass
    if path.is_symlink():
        details += ["", "Link target", os.readlink(path)]
    try:
        if directory:
            with os.scandir(path) as entries:
                count = sum(1 for _entry in entries)
            details += ["", "Entries", str(count)]
            return "text", "Directory\n\nPress Enter to browse it.", "\n".join(details)
        if content_type.startswith("image/"):
            _format, image_width, image_height = GdkPixbuf.Pixbuf.get_file_info(str(path))
            details += ["", "Dimensions", f"{image_width} × {image_height} px"]
            result = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), width, height, True)
            return "image", result, "\n".join(details)
        if content_type == "application/pdf":
            details += ["", "Preview", "First page"]
            return "image", pdf_pixbuf(path, max(width, height)), "\n".join(details)
        with path.open("rb") as stream:
            content = stream.read(300001)
        if b"\0" in content:
            result = "No built-in preview for this file.\n\nPress F4 to open it in its application."
        else:
            result = content[:300000].decode("utf-8", errors="replace")
            result += "\n\n[Preview truncated]" if len(content) > 300000 else ""
        return "text", result, "\n".join(details)
    except Exception as exc:
        return "text", f"Preview unavailable: {exc}\n\nPress F4 to open it in its application.", "\n".join(details)
