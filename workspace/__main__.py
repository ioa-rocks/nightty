# SPDX-License-Identifier: MPL-2.0
"""Launch or present Nightty; --toggle controls the shortcut."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .app import Gdk, Gio, GLib, Gtk, Window
from .model import State


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("directory", nargs="?", help="open this directory in the left pane")
    result.add_argument("--left")
    result.add_argument("--right")
    result.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "surface-workspace")
    result.add_argument("--appearance", type=Path, help="load colors and font sizes from this JSON file")
    result.add_argument("--backend", default="http://127.0.0.1:8098")
    result.add_argument("--maximized", action="store_true", help="open the window maximized")
    result.add_argument("--toggle", action="store_true", help="show or hide the running window")
    result.add_argument("--smoke", action="store_true", help="exercise disposable native fixtures and save screenshots")
    return result


class Application(Gtk.Application):
    def __init__(self, args):
        flags = Gio.ApplicationFlags.NON_UNIQUE if args.smoke else Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        super().__init__(application_id="pw.blackbird.SurfaceWorkspace", flags=flags)
        self.window, self.args, self.result = None, args, 0
        self.client = None

    def do_activate(self):
        if self.window is None:
            state = State(self.args.state)
            self.window = Window(self, self.args, state, client=self.client)
        self.window.show()
        self.window.present()

    def do_command_line(self, command_line):
        args = parser().parse_args(command_line.get_arguments()[1:])
        args.left = args.left or args.directory
        for name in ("left", "right"):
            value = getattr(args, name)
            if value:
                value = Path(value).expanduser()
                setattr(args, name, str(value if value.is_absolute() else Path(command_line.get_cwd()) / value))
        if args.appearance and not args.appearance.is_absolute():
            args.appearance = Path(command_line.get_cwd()) / args.appearance
        if self.window is None:
            self.args = args
        else:
            if args.maximized and not args.smoke and not (args.toggle and self.window.get_visible()):
                self.window.maximize()
            if args.left or args.right:
                if self.window.panes:
                    for pane, folder in zip(self.window.panes, (args.left, args.right)):
                        if folder:
                            pane.navigate(Path(folder))
                else:
                    browser = self.window.current_workspace_tab.workspace_browser
                    paths = list(browser["paths"])
                    for index, folder in enumerate((args.left, args.right)):
                        if folder:
                            paths[index] = Path(folder)
                    browser["paths"] = tuple(paths)
                self.window.show()
                self.window.present()
                return 0
            if args.toggle and self.window.get_visible():
                self.window.hide()
                return 0
        self.activate()
        return 0


def smoke(args):
    """Runs the real widgets with local fixtures, fake agents and one fixture shell."""
    import cairo
    from .app import Pango, Vte

    class FakeAgents:
        calls = []
        interrupt_next = True

        def snapshot(self):
            return {"profiles": [{"id": "fixture", "name": "Research helper", "session_mode": "continuous", "execution": "Fixture", "collection": "public-fixture"}], "sessions": []}

        def create(self, profile):
            self.calls.append(("create", profile))
            return {"id": "fixture-thread"}

        def send(self, profile, session, key, text):
            self.calls.append(("send", profile, session, key, text))
            if self.interrupt_next:
                self.interrupt_next = False
                raise RuntimeError("Fixture interrupted delivery")
            return {"id": session, "status": "completed"}

        def detail(self, profile, session):
            return {"id": session, "status": "completed", "messages": [
                {"role": "assistant", "content": "Fixture response <b>kept as text</b>"}]}

    with tempfile.TemporaryDirectory(prefix="workspace-native-fixture-") as directory:
        root = Path(directory)
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
        image = cairo.ImageSurface(cairo.FORMAT_ARGB32, 900, 480)
        drawing = cairo.Context(image)
        drawing.set_source_rgb(.12, .20, .29)
        drawing.paint()
        for x, y, radius, color in [(180, 235, 90, (.45, .72, .69)), (450, 210, 110, (.82, .65, .40)), (720, 260, 70, (.55, .65, .86))]:
            drawing.set_source_rgb(*color)
            drawing.arc(x, y, radius, 0, 6.284)
            drawing.fill()
        image.write_to_png(str(child / "visual.png"))
        pdf = cairo.PDFSurface(str(child / "sample.pdf"), 400, 300)
        pdf_drawing = cairo.Context(pdf)
        pdf_drawing.set_source_rgb(.1, .2, .3)
        pdf_drawing.paint()
        pdf_drawing.set_source_rgb(1, 1, 1)
        pdf_drawing.select_font_face("Sans")
        pdf_drawing.set_font_size(36)
        pdf_drawing.move_to(50, 160)
        pdf_drawing.show_text("PDF preview")
        pdf.finish()
        args.left, args.right = str(child), str(output)
        args.state = root / "state"
        app = Application(args)
        fake = FakeAgents()
        app.client = fake
        checks = []
        step = {"number": 0, "ticks": 0}

        def screenshot(name):
            window = app.window
            window.check_resize()
            width, height = window.get_allocated_width(), window.get_allocated_height()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            window.draw(cairo.Context(surface))
            surface.write_to_png("/tmp/workspace-" + name + ".png")
            return surface

        def run():
            try:
                step["ticks"] += 1
                if step["ticks"] > 180:
                    current = app.window.panes[0] if app.window else None
                    raise AssertionError(f"Native smoke timed out at step {step['number']}: "
                                         f"path={getattr(current, 'path', None)} "
                                         f"count={current.count.get_text() if current else None} "
                                         f"cursor={current.view.get_cursor() if current else None} "
                                         f"geometry={current.geometry_measured if current else None} "
                                         f"capacity={(current.capacity, current.visible_capacity()) if current else None} "
                                         f"allocations={(current.view.get_allocated_width(), current.view.get_allocated_height()) if current else None} "
                                         f"stack={current.content_stack.get_visible_child_name() if current else None} "
                                         f"scroll={current.scroll.get_vadjustment().get_value() if current else None} "
                                         f"selected={current.selected_paths() if current else None} "
                                         f"quick={app.window.quick_details.get_text() if app.window and app.window.quick_enabled else None} "
                                         f"command_ready={getattr(getattr(app.window, 'command_terminal', None), 'workspace_ready', None)} "
                                         f"command_pending={getattr(getattr(app.window, 'command_terminal', None), 'workspace_pending', None)} "
                                         f"command_text={(app.window.command_terminal.get_text_format(Vte.Format.TEXT) or '')[-250:] if app.window and app.window.command_terminal else None} "
                                         f"expected={step.get('snap_expected')}")
                window = app.window
                if window is None:
                    app.window = Window(app, args, State(args.state), client=fake)
                    window = app.window
                window.stack.set_transition_type(Gtk.StackTransitionType.NONE)
                if step["number"] == 0 and not step.get("startup_checked"):
                    assert window.mode == "console"
                    assert window.stack.get_visible_child_name() == "console"
                    assert len(window.terminals) == 1
                    assert len(window.terminal_tab_bar.get_children()) == 1
                    assert not window.terminal_tabs.get_show_tabs()
                    assert window.terminals[0][0].workspace_tab_button.get_style_context().has_class("top-tab-active")
                    assert window.file_panes is None and not window.panes
                    assert window.gallery_flow is None
                    assert not hasattr(window, "regime_button")
                    assert window.terminals[0][0].workspace_tab_button.get_label() == str(child)
                    assert window.terminals[0][0].workspace_tab_button.get_child().get_max_width_chars() == 24
                    terminal_font = window.terminals[0][0].get_font()
                    assert terminal_font.get_size_is_absolute()
                    assert terminal_font.get_size() == window.appearance["fonts"]["files_px"] * Pango.SCALE
                    assert terminal_font.get_weight() == window.appearance["fonts"]["files_weight"]
                    checks.append("shell startup leaves Files and Gallery unloaded")
                    screenshot("terminal-start")
                    assert window.get_focus() is window.terminals[0][0]
                    key_event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
                    key_event.window = window.terminals[0][0].get_window()
                    key_event.keyval = Gdk.KEY_o
                    key_event.state = Gdk.ModifierType.CONTROL_MASK
                    assert window.terminals[0][0].event(key_event)
                    assert window.mode == "files"
                    assert window.file_panes is not None and len(window.panes) == 2
                    assert window.gallery_flow is None
                    window.terminals[0][0].workspace_tab_button.grab_focus()
                    step["startup_checked"] = True
                    return True
                if step["number"] == 0 and (not window.context or len(window.panes[0].store) < 5
                                            or not window.panes[1].entries
                                            or any(" free" not in pane.folder_stats.get_text()
                                                   for pane in window.panes)
                                            or not window.panes[0].geometry_measured
                                            or window.panes[0].capacity != window.panes[0].visible_capacity()):
                    return True
                if step["number"] == 0 and not step.get("footer_checked"):
                    if not step.get("footer_check_started"):
                        window.panes[0].status.set_text("ドラレコ　人身事故　子ども飛び出し.webm")
                        step["footer_check_started"] = True
                        return True
                    left, right = window.panes
                    left_y = left.list_end.translate_coordinates(window.file_panes, 0, 0)[1]
                    right_y = right.list_end.translate_coordinates(window.file_panes, 0, 0)[1]
                    assert left_y == right_y, (left_y, right_y,
                        left.status_box.get_allocated_height(), right.status_box.get_allocated_height())
                    screenshot("footer-japanese")
                    window.refresh_file_footer(left)
                    step["footer_checked"] = True
                    checks.append("Japanese footer text keeps both pane bottom lines aligned")
                number = step["number"]
                if number == 0:
                    assert window.get_focus() is window.panes[0].view, (
                        window.get_focus(),
                        window.get_focus() is window.panes[0].location,
                        window.get_focus() is window.command_entry)
                    assert window.panes[0].cursor_logical() is not None
                    assert window.panes[0].view.path_is_selected(window.panes[0].view.get_cursor()[1])
                    checks.append("Ctrl+O keeps the file cursor after focus and lazy listing settle")
                    assert window.panes[0].folder_path.get_text() == str(child)
                    assert window.panes[1].folder_path.get_text() == str(output)
                    assert window.status is window.panes[0].status
                    checks.append("each pane shows its own folder status and available space")
                    assert abs(window.file_panes.get_position() /
                               window.file_panes.get_allocated_width() - .5) < .02
                    assert len(window.context["projects"]) == 2
                    assert window.context["agent"] == "fixture"
                    assert not hasattr(window, "sidebar")
                    assert not hasattr(window, "favourites_button")
                    markers = {row[1]: row[0] for row in window.panes[0].store}
                    assert markers[".."] == "/.."
                    assert markers["assets"] == "/assets"
                    assert markers["draft-link"] == "@draft-link"
                    assert markers["run.sh"] == "*run.sh"
                    colors = {row[1]: row[4] for row in window.panes[0].store}
                    assert colors["assets"] == window.palette["directory"]
                    assert colors["mystery.blob"] == window.palette["unknown"]
                    checks.append("compact file type prefixes")
                    window.command_entry.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1]
                    assert window.status is window.panes[1].status
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_gallery_key(None, Gdk.KEY_ISO_Left_Tab, 0,
                        Gdk.ModifierType.SHIFT_MASK)
                    assert window.active is window.panes[0]
                    assert window.get_focus() is window.panes[0].view
                    window.transfers_button.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1]
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_gallery_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[0]
                    assert window.get_focus() is window.panes[0].view
                    checks.append("Tab keeps focus in the file panes from entries and header controls")
                    screenshot("files")
                    pane = window.panes[0]
                    first_mark = next(index for index, row in enumerate(pane.entries)
                                      if row[1] == "draft.md")
                    mark_position = pane.store_position(first_mark)
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([mark_position]), None, False)
                    pane.view.select_path(Gtk.TreePath.new_from_indices([mark_position]))
                    pane.selected()
                    assert str(child) in window.command_prompt.get_text()
                    assert "draft.md" in window.status.get_text()
                    assert pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Insert, state=0))
                    assert pane.cursor_logical() == first_mark + 1
                    assert pane.operation_paths() == [child / "draft.md"]
                    assert pane.move_by_key(pane.view, argparse.Namespace(
                        keyval=Gdk.KEY_t, state=Gdk.ModifierType.CONTROL_MASK))
                    assert len(pane.operation_paths()) == 2
                    assert "2 files selected" in window.status.get_text()
                    assert "entries" in pane.folder_stats.get_text()
                    assert pane.move_by_key(pane.view, argparse.Namespace(
                        keyval=Gdk.KEY_Up, state=Gdk.ModifierType.SHIFT_MASK))
                    assert pane.cursor_logical() == first_mark + 1
                    assert len(pane.operation_paths()) == 3
                    assert "3 files selected" in window.status.get_text()
                    screenshot("files-marked")
                    left_status = pane.status.get_text()
                    window.switch_browser_pane()
                    assert window.status is window.panes[1].status
                    assert pane.status.get_text() == left_status
                    assert "selected" not in window.panes[1].status.get_text()
                    window.switch_browser_pane()
                    pane.marked_paths.clear()
                    pane.view.queue_draw()
                    window.refresh_file_footer(pane)
                    checks.append("MC-style marks stay separate from the cursor and drive copy sources")
                    assert pane.view.get_columns() == 2
                    assert len(pane.entries) > 2 * pane.capacity
                    assert pane.store[0][1] == pane.entries[0][1]
                    assert pane.store[1][1] == pane.entries[pane.capacity][1]
                    assert pane.store[2 * pane.capacity - 2][1] == pane.entries[pane.capacity - 1][1]
                    assert pane.store[2 * pane.capacity][1] == pane.entries[pane.capacity][1]
                    assert pane.store[2 * pane.capacity + 1][1] == pane.entries[2 * pane.capacity][1]
                    step["capacity"] = pane.capacity
                    left_bottom = pane.store_position(pane.capacity - 1, 0)
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([left_bottom]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    assert pane.cursor_logical() == pane.capacity
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Up, state=0))
                    assert pane.cursor_logical() == pane.capacity - 1
                    right_bottom = pane.store_position(2 * pane.capacity - 1, 0)
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([right_bottom]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    assert pane.cursor_logical() == 2 * pane.capacity
                    screenshot("files-next-page")
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([pane.store_position(pane.capacity, 0)]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert pane.cursor_logical() == 2 * pane.capacity
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert pane.cursor_logical() == pane.capacity
                    final_left = len(pane.entries) - pane.capacity - 1
                    pane.scroll.get_vadjustment().set_value(pane.max_top() * pane.item_pitch())
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([
                        pane.store_position(final_left)]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert pane.cursor_logical() == len(pane.entries) - 1
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([pane.store_position(5, 0)]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert pane.cursor_logical() == 0
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([
                        pane.store_position(len(pane.entries) - 1)]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert pane.cursor_logical() == len(pane.entries) - 1
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([pane.store_position(0, 0)]), None, False)
                    window.transfers_button.grab_focus()
                    assert window.browser_arrow_from_control(window.transfers_button,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert pane.cursor_logical() == pane.capacity, (pane.cursor_logical(), pane.capacity,
                                                                   pane.view.has_focus(), window.active.index)
                    checks.append("arrow keys return from controls to the file cursor")
                    checks.append("left and right continue through later columns")
                    adjustment = pane.scroll.get_vadjustment()
                    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                    pane.snap_scroll()
                    assert pane.visible_top() == pane.max_top()
                    top = pane.visible_top()
                    assert top <= pane.cursor_logical() < top + 2 * pane.capacity
                    assert [pane.store[2 * row][3] for row in range(top, top + pane.capacity)] == [
                        pane.entries[logical][3] for logical in range(top, top + pane.capacity)]
                    assert [pane.store[2 * row + 1][3] for row in range(top, top + pane.capacity)] == [
                        pane.entries[logical][3] for logical in range(top + pane.capacity,
                                                                        top + 2 * pane.capacity)]
                    assert pane.store[2 * (top + pane.capacity - 1) + 1][3] == pane.entries[-1][3]
                    checks.append("last file screen keeps both columns full and sorted")
                    screenshot("files-end")
                    overlap = pane.capacity + 5
                    positions = pane.logical_to_stores[overlap]
                    assert len(positions) == 2
                    adjustment.set_value(0)
                    visible_path = Gtk.TreePath.new_from_indices([positions[0]])
                    pane.view.unselect_all()
                    pane.view.set_cursor(visible_path, None, False)
                    pane.view.select_path(visible_path)
                    pane.selected()
                    assert all(pane.view.path_is_selected(Gtk.TreePath.new_from_indices([i]))
                               for i in positions)
                    adjustment.set_value(6 * pane.item_pitch())
                    pane.snap_scroll()
                    pane.view.unselect_path(Gtk.TreePath.new_from_indices([positions[1]]))
                    pane.selected()
                    assert all(not pane.view.path_is_selected(Gtk.TreePath.new_from_indices([i]))
                               for i in positions)
                    checks.append("selection stays in sync across scrolling columns")
                    adjustment.set_value(0)
                    right = window.panes[1]
                    assert right.focus_nearest_on_blank(right.view,
                        argparse.Namespace(button=1, x=right.view.get_allocated_width() * .75, y=700))
                    assert window.active is right and right.cursor_logical() == len(right.entries) - 1
                    window.activate_pane(pane)
                    checks.append("blank panel space focuses the nearest file")
                    window.transfers_button.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_End, 0, Gdk.ModifierType(0))
                    assert pane.cursor_logical() == len(pane.entries) - 1
                    assert window.capture_gallery_key(None, Gdk.KEY_Home, 0, Gdk.ModifierType(0))
                    assert pane.cursor_logical() == 0
                    checks.append("Home and End reach both ends of the file list")
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([0]), None, False)
                    pane.view.select_path(Gtk.TreePath.new_from_indices([0]))
                    pane.view.scroll_to_path(Gtk.TreePath.new_from_indices([0]), True, 0.0, 0.0)
                    checks.append("visible-height column wrap and vertical cursor handoff")
                    pane.view.unselect_all()
                    for filename in ("draft.md", "visual.png"):
                        index = next(i for i, row in enumerate(pane.store) if row[1] == filename)
                        pane.view.select_path(Gtk.TreePath.new_from_indices([index]))
                    assert set(pane.selected_paths()) == {child / "draft.md", child / "visual.png"}
                    pane.view.unselect_all()
                    checks.append("selection across both filename columns")
                    pane.show_search()
                    pane.search.set_text("VISUAL")
                    _has_cursor, cursor, _cell = pane.view.get_cursor()
                    assert pane.store[cursor][1] == "visual.png", (pane.store[cursor][1], pane.search_result.get_text())
                    assert pane.search_result.get_text() == "1 found"
                    screenshot("search")
                    pane.hide_search()
                    checks.append("explicit inline filename search")
                    window.add_favourite()
                    assert child.resolve() in window.state.favourites
                    locations = window.build_locations_menu(window.panes[0])
                    assert any(item.get_label() == str(child.resolve()) for item in locations.get_children() if hasattr(item, "get_label"))
                    window.add_favourite()
                    assert not window.state.favourites
                    window.add_favourite()
                    checks.extend(["nested project context", "inherited agent suggestion", "favourite persistence"])
                    pane.scroll.get_vadjustment().set_value(pane.max_top() * pane.item_pitch())
                    pane.snap_scroll()
                    image_row = next(i for i, row in enumerate(window.panes[0].store) if row[1] == "visual.png"
                                     and pane.visible_top() <= i // 2 < pane.visible_top() + pane.capacity)
                    window.panes[0].view.unselect_all()
                    window.panes[0].view.set_cursor(Gtk.TreePath.new_from_indices([image_row]), None, False)
                    window.panes[0].view.select_path(Gtk.TreePath.new_from_indices([image_row]))
                    window.panes[0].selected()
                    window.toggle_quick_view()
                    step["number"] = 1
                elif number == 1:
                    if window.panes[0].capacity != window.panes[0].visible_capacity():
                        return True
                    if (window.quick_image.get_pixbuf() is None
                            or "900 × 480 px" not in window.quick_details.get_text()):
                        return True
                    assert window.right_stack.get_visible_child_name() == "quick"
                    assert window.panes[1].path == output
                    checks.append("right-pane quick view preserves copy destination")
                    screenshot("quick-view")
                    window.toggle_quick_view()
                    pane = window.panes[0]
                    adjustment = pane.scroll.get_vadjustment()
                    expected = 3 * pane.item_pitch()
                    adjustment.set_value(expected + pane.item_pitch() // 3)
                    pane.snap_scroll()
                    assert abs(adjustment.get_value() - expected) < .5
                    screenshot("files-scrolled")
                    checks.append("file scrolling lands on complete rows")
                    window.set_mode("gallery")
                    assert window.get_focus() is window.gallery_flow
                    step["number"] = 2
                elif number == 2:
                    window.check_resize()
                    pane = window.active
                    if pane.gallery_resize_source:
                        return True
                    if window.gallery_layout(pane) != (pane.gallery_columns, pane.gallery_capacity):
                        window.apply_gallery_resize(pane)
                        return True
                    matches = [item for item in window.active.gallery_children_by_logical
                               if item is not None and item.file_path.name == "visual.png"]
                    if not matches or matches[0].preview_image.get_pixbuf() is None:
                        return True
                    parent_tile = window.gallery_flow.get_children()[0]
                    assert parent_tile.is_parent and isinstance(parent_tile.get_child(), Gtk.Box)
                    assert parent_tile.get_child().get_children()[0].get_text() == ".."
                    parent_label = parent_tile.get_child().get_children()[0]
                    window.check_resize()
                    before = parent_label.translate_coordinates(window.gallery_flow, 0, 0)
                    window.gallery_flow.select_child(parent_tile)
                    window.check_resize()
                    assert parent_label.translate_coordinates(window.gallery_flow, 0, 0) == before, (
                        before, parent_label.translate_coordinates(window.gallery_flow, 0, 0))
                    parent_tile.grab_focus()
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert window.gallery_flow.get_selected_children() == [parent_tile]
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert window.gallery_flow.get_selected_children()[0] is window.active.gallery_children_by_logical[
                        window.active.gallery_capacity]
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert window.gallery_flow.get_selected_children() == [parent_tile]
                    assert window.capture_gallery_key(None, Gdk.KEY_End, 0, Gdk.ModifierType(0))
                    assert window.gallery_selected_paths() == [child / "zz-track.mp3"]
                    assert window.capture_gallery_key(None, Gdk.KEY_Home, 0, Gdk.ModifierType(0))
                    assert window.gallery_flow.get_selected_children() == [parent_tile]
                    checks.append("Home and End reach both ends of the gallery")
                    window.gallery_flow.unselect_all()
                    audio_tile = next(item for item in window.active.gallery_children_by_logical
                                      if item is not None and item.file_path.name == "zz-track.mp3")
                    if audio_tile.preview_image.get_storage_type() != Gtk.ImageType.GICON:
                        return True
                    assert audio_tile.preview_image.get_gicon()[0].equal(
                        Gio.content_type_get_icon(Gio.content_type_guess(str(child / "zz-track.mp3"), None)[0]))
                    window.scroll_gallery_to(matches[0])
                    if window.active.gallery_scroll.get_vadjustment().get_value() <= 0:
                        return True
                    visible_match = window.gallery_child_for_index(
                        window.active, matches[0].gallery_index, window.gallery_top(window.active))
                    assert window.gallery_click(window.active, visible_match,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), visible_match)
                    assert window.gallery_selected_paths() == [child / "visual.png"]
                    assert window.active.selected_paths() == [child / "visual.png"]
                    visible_match.grab_focus()
                    assert window.stack.get_visible_child_name() == "files"
                    assert window.active.content_stack.get_visible_child_name() == "gallery"
                    assert window.panes[1].content_stack.get_visible_child_name() == "files"
                    assert matches[0].get_allocated_width() > matches[0].get_allocated_height()
                    checks.append("gallery uses media previews, file type icons and a stable parent card")
                    window.command_entry.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1] and window.mode == "files"
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_gallery_key(None, Gdk.KEY_ISO_Left_Tab, 0,
                        Gdk.ModifierType.SHIFT_MASK)
                    assert window.active is window.panes[0] and window.mode == "gallery"
                    focus = window.get_focus()
                    assert focus is window.gallery_flow or focus.is_ancestor(window.gallery_flow)
                    checks.append("Tab keeps focus in mixed Gallery and Files panes")
                    window.active.gallery_scroll.get_vadjustment().set_value(0)
                    surface = screenshot("gallery-top")
                    gallery_pane = window.active
                    first_partial = window.gallery_child_for_index(
                        gallery_pane, gallery_pane.gallery_capacity, gallery_pane.gallery_capacity)
                    assert first_partial.gallery_row == gallery_pane.gallery_capacity
                    covered_x, covered_y = first_partial.translate_coordinates(window, 20, 20)
                    _scroll_x, scroll_y = first_partial.translate_coordinates(
                        gallery_pane.gallery_scroll, 20, 20)
                    assert scroll_y > window.gallery_visible_bottom(gallery_pane)
                    pixel = bytes(surface.get_data())[(covered_y * surface.get_stride() + covered_x * 4):
                                                      (covered_y * surface.get_stride() + covered_x * 4 + 4)]
                    background = window.palette["background"]
                    assert pixel[:3] == bytes.fromhex(background[5:7] + background[3:5] + background[1:3]), pixel
                    assert window.gallery_click(gallery_pane, first_partial,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), first_partial)
                    assert all(item.gallery_row < gallery_pane.gallery_capacity
                               for item in gallery_pane.gallery_flow.get_selected_children())
                    _columns, pitch, _first_y = window.gallery_geometry(
                        gallery_pane.gallery_flow, gallery_pane.gallery_flow.get_children())
                    gallery_pane.gallery_scroll.get_vadjustment().set_value(3 * pitch)
                    window.snap_gallery_scroll(gallery_pane)
                    assert window.gallery_top(gallery_pane) == 3
                    for row in range(3, 3 + gallery_pane.gallery_capacity):
                        visible = [item.gallery_index for item in gallery_pane.gallery_flow.get_children()
                                   if getattr(item, "gallery_row", -1) == row]
                        assert visible == [row, row + gallery_pane.gallery_capacity], (row, visible)
                    duplicate = window.gallery_child_for_index(
                        gallery_pane, gallery_pane.gallery_capacity + 1, 3)
                    assert duplicate.gallery_row == gallery_pane.gallery_capacity + 1
                    assert window.gallery_click(gallery_pane, duplicate,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), duplicate)
                    assert window.gallery_top(gallery_pane) == 3
                    assert window.gallery_selected_paths() == [duplicate.file_path]
                    checks.append("gallery scrolling keeps consecutive columns and masks partial cards")
                    window.scroll_gallery_to(matches[0])
                    screenshot("gallery")
                    logical_children = window.active.gallery_children_by_logical
                    capacity = window.active.gallery_capacity
                    last_top = window.gallery_max_top(window.active)
                    left_bottom = window.gallery_child_for_index(
                        window.active, min(last_top + capacity, len(logical_children)) - 1, last_top)
                    last_right = window.gallery_child_for_index(
                        window.active, len(logical_children) - 1, last_top)
                    assert left_bottom.get_allocation().x < last_right.get_allocation().x, (
                        len(logical_children), capacity, window.active.gallery_columns,
                        left_bottom.gallery_index, left_bottom.get_allocation().x,
                        last_right.gallery_index, last_right.get_allocation().x)
                    window.gallery_flow.unselect_all()
                    window.gallery_flow.select_child(left_bottom)
                    left_bottom.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))
                    assert window.gallery_selected_paths() == [child / "zz-track.mp3"]
                    assert window.active is window.panes[0]
                    window.gallery_flow.unselect_all()
                    window.gallery_flow.select_child(left_bottom)
                    left_bottom.grab_focus()
                    window.transfers_button.grab_focus()
                    assert window.browser_arrow_from_control(window.transfers_button,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert window.gallery_selected_paths() == [child / "zz-track.mp3"]
                    assert window.active is window.panes[0]
                    assert window.panes[1].content_stack.get_visible_child_name() == "files"
                    window.gallery_flow.unselect_all()
                    window.gallery_flow.select_child(matches[0])
                    matches[0].grab_focus()
                    checks.append("gallery horizontal keys follow file-view columns without moving the panel")
                    opened = []
                    original_open_external = window.open_external
                    window.open_external = lambda path=None: opened.append(path)
                    try:
                        window.gallery_activated(window.gallery_flow, matches[0])
                        file_row = next(i for i, row in enumerate(window.panes[0].store)
                                        if row[1] == "visual.png")
                        window.panes[0].open(window.panes[0].view,
                            Gtk.TreePath.new_from_indices([file_row]))
                    finally:
                        window.open_external = original_open_external
                    assert opened == [child / "visual.png", child / "visual.png"]
                    assert window.mode == "gallery"
                    checks.append("Enter opens the default application in Files and Gallery")
                    window.job = window.start_copy(window.gallery_selected_paths(), output)
                    assert window.key(window, argparse.Namespace(keyval=Gdk.KEY_F3, state=0))
                    step["number"] = 3
                elif number == 3:
                    if window.job.status != "completed" or "Dimensions" not in window.inspector_details.get_text():
                        return True
                    assert window.mode == "inspect", window.mode
                    assert window.preview_stack.get_visible_child_name() == "image"
                    assert "900 × 480 px" in window.inspector_details.get_text()
                    assert (output / "visual.png").read_bytes() == (child / "visual.png").read_bytes()
                    assert window.panes[1].path == output
                    checks.extend(["gallery copy", "expanded image and metadata", "pane positions preserved"])
                    screenshot("inspect-image")
                    window.close_inspector()
                    assert window.mode == "gallery"
                    assert window.gallery_selected_paths() == [child / "visual.png"]
                    assert window.active.selected_paths() == [child / "visual.png"]
                    window.preview(child / "README.md")
                    step["number"] = 4
                elif number == 4:
                    if "Modified" not in window.inspector_details.get_text():
                        return True
                    assert window.preview_stack.get_visible_child_name() == "text"
                    assert "A working draft" in window.document.get_buffer().get_text(
                        window.document.get_buffer().get_start_iter(), window.document.get_buffer().get_end_iter(), False)
                    checks.append("expanded text and metadata")
                    window.preview(child / "sample.pdf")
                    step["number"] = 5
                elif number == 5:
                    if "First page" not in window.inspector_details.get_text():
                        return True
                    assert window.preview_stack.get_visible_child_name() == "image"
                    assert window.image.get_pixbuf() is not None
                    checks.append("PDF first-page preview")
                    screenshot("inspect-pdf")
                    window.set_mode("project")
                    screenshot("project")
                    window.new_terminal(command=["/bin/sh", "-c", "printf 'SURFACE_TERMINAL_OK\\n'; exec /bin/sh"])
                    window.set_mode("console")
                    step["number"] = 6
                elif number == 6:
                    terminal = window.terminals[-1][0]
                    text = terminal.get_text_format(Vte.Format.TEXT) or ""
                    if "SURFACE_TERMINAL_OK" not in [line.strip() for line in text.splitlines()]:
                        return True
                    shell_tab = window.terminals[1][0]
                    shell_tab.workspace_browser["paths"] = (bulk, output)
                    generation = tuple(pane.generation for pane in window.panes)
                    window.select_terminal(0)
                    window.select_terminal(1)
                    assert tuple(pane.generation for pane in window.panes) == generation
                    assert window.panes[0].path == child
                    shell_tab.workspace_browser["paths"] = (child, output)
                    checks.append("shell tabs do not scan unopened file views")
                    window.hide()
                    assert any(pid for _terminal, pid in window.terminals)
                    app.activate()
                    assert window.get_visible()
                    checks.extend(["embedded terminal output", "hide preserves process"])
                    screenshot("console")
                    assert len(window.terminals) == 2
                    assert window.top_tab_shortcut("Page_Up", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "project" and window.terminal_tabs.get_current_page() == 0
                    assert window.top_tab_shortcut("Page_Down", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "console" and window.terminal_tabs.get_current_page() == 1
                    assert window.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_2, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "project"
                    assert window.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_3, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    checks.append("idle shell shortcuts open native views directly")
                    window.set_mode("files")
                    assert window.top_tab_shortcut("Page_Up", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "project"
                    assert window.top_tab_shortcut("Page_Down", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "files" and window.terminal_tabs.get_current_page() == 1
                    checks.append("Ctrl+PageUp and Ctrl+PageDown retain each tab's view")
                    window.show_jobs()
                    assert window.transfer_popover.get_visible()
                    assert "COMPLETED" in window.jobs_label.get_text()
                    window.show_jobs()
                    checks.append("transfers open in a compact dropdown")
                    window.set_mode("project")
                    window.project_tabs.set_current_page(2)
                    step["number"] = 7
                elif number == 7:
                    if window.profile_picker.get_active_id() != "fixture":
                        return True
                    assert not fake.calls
                    window.compose.get_buffer().set_text("Inspect this fixture")
                    window.send_agent()
                    step["number"] = 8
                elif number == 8:
                    if window.agent_sending:
                        return True
                    # Refresh can replace the composer buffer; the retry must
                    # still keep its previously created session and request ID.
                    window.refresh_agents()
                    step["number"] = 9
                elif number == 9:
                    if window.agent_loading or window.profile_picker.get_active_id() != "fixture":
                        return True
                    window.send_agent()
                    step["number"] = 10
                elif number == 10:
                    text = window.chat.get_buffer().get_text(window.chat.get_buffer().get_start_iter(), window.chat.get_buffer().get_end_iter(), False)
                    if "Fixture response" not in text:
                        return True
                    assert "<b>kept as text</b>" in text
                    creates = [call for call in fake.calls if call[0] == "create"]
                    sends = [call for call in fake.calls if call[0] == "send"]
                    assert len(creates) == 1 and len(sends) == 2 and sends[0] == sends[1]
                    checks.extend(["no inference on navigation", "explicit agent send", "native response rendering"])
                    checks.append("interrupted delivery retains session and request identity")
                    screenshot("agents")
                    window.set_mode("files")
                    window.panes[0].scroll.get_vadjustment().set_value(0)
                    step["parent_scroll_samples"] = []
                    window.panes[0].scroll.get_vadjustment().connect("value-changed",
                        lambda adjustment: step["parent_scroll_samples"].append(
                            (round(adjustment.get_value(), 2), GLib.get_monotonic_time())))
                    window.panes[0].up()
                    step["number"] = 11
                elif number == 11:
                    if window.panes[0].path != project or "Loading" in window.panes[0].count.get_text():
                        return True
                    _has_cursor, cursor, _cell = window.panes[0].view.get_cursor()
                    if cursor is None:
                        return True
                    assert window.panes[0].store[cursor][1] == child.name, (
                        window.panes[0].store[cursor][1], window.panes[0].pending_restore_name,
                        window.panes[0].count.get_text(), window.panes[0].cursor_logical())
                    samples = step["parent_scroll_samples"]
                    assert samples and samples[-1][0] % window.panes[0].item_pitch() == 0, samples
                    if len(samples) > 1:
                        assert samples[-1][1] - samples[-2][1] < 30000, samples
                    checks.append("parent navigation keeps cursor on the folder left")
                    checks.append("parent navigation aligns rows before the next frame")
                    window.panes[0].navigate(child)
                    step["number"] = 12
                elif number == 12:
                    if window.panes[0].path != child:
                        return True
                    window.command_entry.set_text("")
                    window.command_key(argparse.Namespace(keyval=Gdk.KEY_c, state=0))
                    assert window.command_entry.get_text() == "c"
                    window.command_entry.set_text("cd assets")
                    window.execute_command_line()
                    step["number"] = 13
                elif number == 13:
                    if window.panes[0].path != child / "assets":
                        return True
                    assert window.mode == "files"
                    window.command_entry.set_text("cd -")
                    window.execute_command_line()
                    step["number"] = 14
                elif number == 14:
                    if window.panes[0].path != child:
                        return True
                    checks.append("command line typing and cd follow the active pane")
                    window.command_entry.set_text("printf 'WORKSPACE_COMMAND_OK\\n'; pwd")
                    window.execute_command_line()
                    assert window.mode == "console"
                    assert window.stack.get_visible_child_name() == "console"
                    assert window.stack.get_child_by_name("console") is window.terminal_box
                    step["number"] = 15
                elif number == 15:
                    terminal = window.command_terminal
                    terminal_text = terminal.get_text_format(Vte.Format.TEXT) or ""
                    if "WORKSPACE_COMMAND_OK" not in terminal_text or str(child) not in terminal_text:
                        return True
                    assert "cd --" not in terminal_text
                    screenshot("command-output")
                    assert window.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.command_row.get_visible()
                    assert window.stack.get_visible_child_name() == "files"
                    window.command_entry_key(window.command_entry, argparse.Namespace(keyval=Gdk.KEY_Up, state=0))
                    assert window.command_entry.get_text().startswith("printf ")
                    window.command_entry_key(window.command_entry, argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    assert not window.command_entry.get_text()
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    assert window.stack.get_visible_child_name() == "console"
                    assert "WORKSPACE_COMMAND_OK" in terminal.get_text_format(Vte.Format.TEXT)
                    checks.append("persistent full-window shell output and Ctrl+O panel toggle")
                    assert not window.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_p, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    checks.append("terminal Ctrl+P remains available to shell programs")
                    window.set_mode("project")
                    assert window.mode == "project"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.get_visible() and window.mode == "project"
                    window.mode_shortcut("p")
                    assert window.mode == "console"
                    window.set_mode("files")
                    assert window.command_entry_key(window.command_entry,
                        argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "gallery"
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[1]
                    assert window.mode == "files"
                    assert window.panes[1].content_stack.get_visible_child_name() == "files"
                    assert window.panes[0].content_stack.get_visible_child_name() == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "gallery"
                    assert all(pane.content_stack.get_visible_child_name() == "gallery" for pane in window.panes)
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[0] and window.mode == "gallery"
                    assert all(pane.content_stack.get_visible_child_name() == "gallery" for pane in window.panes)
                    assert window.gallery_key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[1] and window.mode == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[0]
                    assert window.mode == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.mode == "gallery" and window.get_visible()
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.get_visible()
                    pane = window.active
                    row = next(i for i, entry in enumerate(pane.store) if entry[1] == "visual.png")
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([row]), None, False)
                    pane.view.select_path(Gtk.TreePath.new_from_indices([row]))
                    path = str(child / "visual.png")
                    pane.marked_paths.discard(path)
                    command_text = window.command_entry.get_text()
                    window.command_entry.grab_focus()
                    assert window.capture_gallery_key(None, Gdk.KEY_Insert, 0, Gdk.ModifierType(0))
                    assert path in pane.marked_paths
                    assert window.get_focus() is pane.view
                    assert window.command_entry.get_text() == command_text
                    pane.marked_paths.discard(path)
                    window.refresh_file_footer(pane)
                    checks.append("Insert marks files after Gallery without moving focus to the command line")
                    checks.append("Ctrl+P and Ctrl+I switch modes; Escape preserves persistent views")
                    window.set_mode("gallery")
                    window.panes[0].navigate(child / "assets")
                    step["number"] = 16
                elif number == 16:
                    if window.panes[0].path != child / "assets" or not window.gallery_flow.get_children():
                        return True
                    assert window.mode == "gallery"
                    assert window.gallery_flow.get_children()[0].is_parent
                    assert window.gallery_click(window.active, window.active.gallery_scroll,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=500, y=700))
                    assert window.gallery_flow.get_selected_children()[0].is_parent
                    checks.append("blank gallery space focuses the nearest card")
                    window.panes[0].navigate(child)
                    step["number"] = 17
                elif number == 17:
                    if window.panes[0].path != child or not any(
                            item is not None and item.file_path.name == "visual.png"
                            for item in window.active.gallery_children_by_logical):
                        return True
                    assert window.mode == "gallery"
                    checks.append("gallery folder navigation refreshes without losing selection state")
                    window.set_mode("files")
                    pane = window.panes[0]
                    original_rebuild = pane.rebuild_entries
                    step["original_rebuild"] = original_rebuild

                    def observe_large_rebuild(*rebuild_args, **rebuild_kwargs):
                        result = original_rebuild(*rebuild_args, **rebuild_kwargs)
                        if pane.path == bulk and not step.get("bulk_first_seen"):
                            assert len(pane.entries) > 256
                            assert pane.cursor_logical() == 0
                            assert len(pane.store) < len(pane.entries)
                            step["bulk_first_seen"] = True
                            pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_End, state=0))
                            assert pane.model_pending_target == len(pane.entries) - 1
                        return result

                    pane.rebuild_entries = observe_large_rebuild
                    pane.navigate(bulk)
                    step["number"] = 18
                elif number == 18:
                    pane = window.panes[0]
                    if pane.path != bulk or pane.model_fill_source:
                        return True
                    assert step.get("bulk_first_seen")
                    assert len(pane.logical_to_stores) == len(pane.entries)
                    assert pane.cursor_logical() == len(pane.entries) - 1
                    assert pane.selected_paths() == [bulk / "program-0899"]
                    checks.append("large folders show a cursor before later rows load")
                    pane.rebuild_entries = step["original_rebuild"]
                    step["bin_started"] = time.monotonic()
                    pane.navigate(Path("/bin"))
                    step["number"] = 19
                elif number == 19:
                    pane = window.panes[0]
                    if pane.path != Path("/bin"):
                        return True
                    assert pane.cursor_logical() == 0
                    assert pane.view.get_cursor()[0]
                    checks.append("/bin keeps a visible cursor while loading")
                    screenshot("bin")
                    step["number"] = 20
                elif number == 20:
                    pane = window.panes[0]
                    if pane.model_fill_source:
                        return True
                    assert len(pane.logical_to_stores) == len(pane.entries)
                    assert pane.view.path_is_selected(pane.view.get_cursor()[1])
                    step["bin_loaded_seconds"] = round(time.monotonic() - step["bin_started"], 2)
                    checks.append("/bin finishes loading without losing navigation")
                    from . import app as app_module
                    original_listing = app_module.directory_entries

                    def delayed_listing(folder, hidden=False, on_batch=None, cancelled=None):
                        if Path(folder) != slow:
                            return original_listing(folder, hidden, on_batch, cancelled)

                        def delayed_batch(batch):
                            on_batch(batch)
                            time.sleep(0.2)

                        return original_listing(folder, hidden, delayed_batch, cancelled)

                    app_module.directory_entries = delayed_listing
                    window.panes[0].navigate(slow)
                    step["number"] = 21
                elif number == 21:
                    pane = window.panes[0]
                    if pane.path != slow or len(pane.entries) <= 1:
                        return True
                    assert "loading" in pane.count.get_text().lower()
                    assert pane.cursor_logical() == 0
                    assert len(pane.entries) < 401
                    checks.append("slow scans expose partial results with a cursor")
                    screenshot("slow-partial")
                    step["number"] = 22
                elif number == 22:
                    pane = window.panes[0]
                    if "loading" in pane.count.get_text().lower() or pane.model_fill_source:
                        return True
                    assert len(pane.entries) == 401
                    assert pane.cursor_logical() == 0
                    checks.append("slow scans complete without blocking the pane")
                    window.command_entry.set_text(f"cd {bulk}")
                    window.execute_command_line()
                    assert window.status.get_text().startswith("Checking ")
                    step["number"] = 23
                elif number == 23:
                    pane = window.panes[0]
                    if pane.path != bulk or "loading" in pane.count.get_text().lower():
                        return True
                    if not window.terminals[0][0].workspace_ready:
                        return True
                    assert pane.cursor_logical() is not None
                    checks.append("typed cd validates unknown folders off the GTK thread")
                    window.select_terminal(0)
                    assert window.current_workspace_tab.workspace_browser["paths"][0] == child
                    assert window.project_context_path == child
                    window.select_terminal(1)
                    assert window.panes[0].path == bulk
                    checks.append("workspace tabs retain independent browser folders")
                    tab = window.terminals[1][0]
                    folder = child / "assets"
                    window.panes[0].navigate(folder)
                    window.set_mode("console")
                    assert tab.workspace_cwd == folder
                    assert tab.workspace_tab_button.get_label() == str(folder)
                    pending = len(tab.workspace_pending)
                    window.terminal_shortcut(tab,
                        argparse.Namespace(keyval=Gdk.KEY_Control_L, state=Gdk.ModifierType.CONTROL_MASK))
                    window.set_mode("files")
                    window.set_mode("console")
                    assert len(tab.workspace_pending) == pending
                    checks.append("folder sync happens once across repeated Ctrl+O toggles")
                    window.select_terminal(0)
                    window.set_mode("console")
                    shell = window.terminals[0][0]
                    shell.feed_child(b"sleep 2\n")
                    step["number"] = 24
                elif number == 24:
                    shell = window.terminals[0][0]
                    if not window.terminal_has_foreground_program(shell):
                        return True
                    assert not window.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    checks.append("Ctrl+O reaches a foreground terminal program")
                    step["number"] = 25
                elif number == 25:
                    shell = window.terminals[0][0]
                    if window.terminal_has_foreground_program(shell):
                        return True
                    assert window.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "project"
                    checks.append("Ctrl+O switches Nightty views at the shell prompt")
                    assert window.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o,
                                           state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
                    assert window.mode == "project"
                    checks.append("Ctrl+Shift+O sends a literal key to the terminal")
                    assert window.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_Return, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_Return, state=Gdk.ModifierType.SHIFT_MASK))
                    assert shell.get_allow_hyperlink()
                    assert shell.workspace_url_tag >= 0 and shell.workspace_path_tag >= 0
                    checks.append("Ctrl+Enter and Shift+Enter reach the shell as newlines; terminal links register")
                    step["short_tab"] = window.new_terminal(command=["/bin/sh", "-c", "cd /; exec /bin/sh"])
                    step["number"] = 26
                elif number == 26:
                    short_tab = step["short_tab"]
                    window.refresh_mode_buttons()
                    if short_tab.workspace_tab_button.get_label() != "/":
                        return True
                    checks.append("tab paths follow shell directory changes")
                    short_tab.feed_child(b"exit\n")
                    step["number"] = 27
                elif number == 27:
                    short_tab = step["short_tab"]
                    if short_tab in [terminal for terminal, _pid in window.terminals]:
                        return True
                    assert len(window.terminals) == 2
                    assert window.current_workspace_tab is window.terminals[1][0]
                    checks.append("exiting shell tabs close without changing other tabs")
                    window.activate_pane(window.panes[0])
                    window.panes[0].navigate(child)
                    window.set_mode("gallery")
                    window.resize(1400, 560)
                    step["number"] = 28
                elif number == 28:
                    pane = window.panes[0]
                    window.check_resize()
                    if (pane.path != child or len(pane.gallery_children_by_logical) < 70
                            or any(item is None for item in pane.gallery_children_by_logical)
                            or pane.gallery_scroll.get_allocated_height() > 600
                            or pane.gallery_resize_source):
                        return True
                    if window.gallery_layout(pane) != (pane.gallery_columns, pane.gallery_capacity):
                        window.apply_gallery_resize(pane)
                        return True
                    assert pane.gallery_columns == 2
                    step["small_gallery_capacity"] = pane.gallery_capacity
                    step["small_gallery_height"] = pane.gallery_scroll.get_allocated_height()
                    current = pane.gallery_children_by_logical[pane.gallery_capacity - 1]
                    pane.gallery_flow.unselect_all()
                    pane.gallery_flow.select_child(current)
                    current.grab_focus()
                    window.resize(1400, 940)
                    step["number"] = 29
                elif number == 29:
                    pane = window.panes[0]
                    window.check_resize()
                    if (pane.gallery_scroll.get_allocated_height() <= step["small_gallery_height"] + 100
                            or pane.gallery_capacity <= step["small_gallery_capacity"]
                            or pane.gallery_resize_source
                            or any(item is None for item in pane.gallery_children_by_logical)):
                        return True
                    if window.gallery_layout(pane) != (pane.gallery_columns, pane.gallery_capacity):
                        window.apply_gallery_resize(pane)
                        return True
                    old_bottom = pane.gallery_children_by_logical[step["small_gallery_capacity"] - 1]
                    assert pane.gallery_flow.get_selected_children() == [old_bottom]
                    assert window.gallery_key(pane.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    next_card = window.gallery_child_for_index(
                        pane, step["small_gallery_capacity"], window.gallery_top(pane))
                    assert next_card in pane.gallery_flow.get_selected_children()
                    assert next_card.get_allocation().x == old_bottom.get_allocation().x
                    screenshot("gallery-resized")
                    checks.append("Gallery Down follows the resized column to its new bottom")
                    window.set_mode("files")
                    step["old_split_width"] = window.file_panes.get_allocated_width()
                    window.resize(1600, 940)
                    step["number"] = 30
                elif number == 30:
                    window.check_resize()
                    width = window.file_panes.get_allocated_width()
                    if width < step["old_split_width"] + 100:
                        return True
                    assert abs(window.file_panes.get_position() / width - .5) < .02
                    window.file_panes.set_position(round(width * .65))
                    assert abs(window.pane_split_ratio - .65) < .02
                    window.resize(1200, 940)
                    step["number"] = 31
                elif number == 31:
                    window.check_resize()
                    width = window.file_panes.get_allocated_width()
                    if width > 1300:
                        return True
                    assert abs(window.file_panes.get_position() / width - .65) < .02
                    checks.append("panes start evenly and retain an intentional split across resize")
                    step["old_file_capacity"] = window.panes[0].capacity
                    window.resize(1200, 560)
                    step["number"] = 32
                elif number == 32:
                    window.check_resize()
                    pane = window.panes[0]
                    if (pane.capacity >= step["old_file_capacity"]
                            or pane.capacity != pane.visible_capacity()
                            or pane.model_fill_source):
                        return True
                    adjustment = pane.scroll.get_vadjustment()
                    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                    pane.snap_scroll()
                    top = pane.visible_top()
                    assert top == pane.max_top()
                    assert pane.store[2 * (top + pane.capacity - 1) + 1][3] == pane.entries[-1][3]
                    checks.append("file columns stay full after a height resize")
                    adjustment = pane.scroll.get_vadjustment()
                    pitch = pane.item_pitch()
                    aligned_offset = (pane.top_inset - pane.scroll.get_allocated_height()) % pitch
                    adjustment.set_value(3 * pitch + aligned_offset)
                    screenshot("row-boundary")
                    step["number"] = 33
                elif number == 33:
                    assert len(window.terminals) == 2
                    step["row_geometry"] = {"top_inset": window.panes[0].top_inset,
                                            "pitch": window.panes[0].row_pitch,
                                            "capacity": window.panes[0].capacity}
                    window.terminals[0][0].feed_child(b"exit\n")
                    step["number"] = 34
                elif number == 34:
                    if len(window.terminals) != 1:
                        return True
                    checks.append("exiting a nonfinal shell keeps Nightty open")
                    window.terminals[0][0].feed_child(b"exit\n")
                    step["number"] = 35
                elif number == 35:
                    return True
                return True
            except Exception:
                import traceback
                traceback.print_exc()
                app.result = 1
                app.quit()
                return False

        GLib.timeout_add(150, run)
        app.run([sys.argv[0]])
        if app.result == 0:
            assert step["number"] == 35 and not app.window.terminals
            checks.append("exiting the final shell quits Nightty")
            print(json.dumps({"checks": checks,
                "bin_loaded_seconds": step["bin_loaded_seconds"],
                "row_geometry": step["row_geometry"],
                "screenshots": ["/tmp/workspace-" + mode + ".png" for mode in ("terminal-start", "footer-japanese", "files", "files-marked", "files-next-page", "files-end", "files-scrolled", "row-boundary", "search", "quick-view", "gallery-top", "gallery", "gallery-resized", "inspect-image", "inspect-pdf", "project", "console", "agents", "command-output", "bin", "slow-partial")]}), flush=True)
        return app.result


def main():
    args = parser().parse_args()
    args.left = args.left or args.directory
    if args.smoke:
        return smoke(args)
    app = Application(args)
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
