# SPDX-License-Identifier: MPL-2.0
"""Thumbnail and inspector renderers for files that need an external decoder."""

import subprocess

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf


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
