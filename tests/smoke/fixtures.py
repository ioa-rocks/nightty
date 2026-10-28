# SPDX-License-Identifier: MPL-2.0
"""Disposable folder tree the smoke scenario navigates, copies into and previews."""

from dataclasses import dataclass
import json
from pathlib import Path

import cairo


@dataclass(frozen=True)
class Fixtures:
    root: Path
    project: Path
    child: Path
    output: Path
    bulk: Path
    slow: Path


def build(root):
    """Create the fixture tree under root and return its notable paths."""
    project, output, bulk, slow = (root / "Publishing", root / "Outbox",
                                   root / "Bulk", root / "Slow")
    child = project / "Z language article"
    child.mkdir(parents=True)
    bulk.mkdir()
    slow.mkdir()
    for index in range(900):
        (bulk / f"program-{index:04}").touch()
    for index in range(400):
        (slow / f"remote-{index:04}").touch()
    for index in range(65):
        (project / f"Sibling-{index:02}").mkdir()
    output.mkdir()
    (project / ".surface.json").write_text(json.dumps({"type": "project", "title": "Publishing", "description": "Field notes, technical writing and language.", "agent": "fixture"}))
    (child / ".surface.json").write_text(json.dumps({"type": "project", "title": "A language article", "description": "Explain an interesting phrase with evidence.", "work": ["check-the-claim.md"]}))
    (child / "README.md").write_text("# A language article\n\nA working draft with sources and one outstanding claim.\n\n## Next action\nCheck the evidence before revising the opening.\n")
    (child / "draft.md").write_text("# A word worth noticing\n\nA draft opened directly from its project folder.\n")
    (child / "check-the-claim.md").write_text("# Check the claim\n\nWaiting for a reliable dictionary citation.\n")
    (child / "assets").mkdir()
    (child / "draft-link").symlink_to("draft.md")
    (child / "mystery.blob").write_bytes(b"\x00\x01")
    (child / "zz-track.mp3").write_bytes(b"ID3 audio fixture")
    script = child / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (child / "0-日本語.txt").write_text("Japanese filename\n")
    for index in range(80):
        (child / f"note-{index:02}.txt").write_text(f"Note {index}\n")
    (child / "note-16-日本語.txt").write_text("Japanese name in the right column\n")
    image = cairo.ImageSurface(cairo.FORMAT_ARGB32, 900, 480)
    drawing = cairo.Context(image)
    drawing.set_source_rgb(.12, .20, .29)
    drawing.paint()
    for x, y, radius, color in [(180, 235, 90, (.45, .72, .69)), (450, 210, 110, (.82, .65, .40)), (720, 260, 70, (.55, .65, .86))]:
        drawing.set_source_rgb(*color)
        drawing.arc(x, y, radius, 0, 6.284)
        drawing.fill()
    image.write_to_png(str(child / "visual.png"))
    pdf = cairo.PDFSurface(str(child / "sample.pdf"), 300, 400)
    pdf_drawing = cairo.Context(pdf)
    pdf_drawing.set_source_rgb(.1, .2, .3)
    pdf_drawing.paint()
    pdf_drawing.set_source_rgb(1, 1, 1)
    pdf_drawing.select_font_face("Sans")
    pdf_drawing.set_font_size(36)
    pdf_drawing.move_to(50, 160)
    pdf_drawing.show_text("PDF preview")
    pdf.finish()
    return Fixtures(root, project, child, output, bulk, slow)
