# SPDX-License-Identifier: MPL-2.0
"""Drive the real Nightty widgets through navigation, gallery, transfers, terminals and agents.

The scenario is a step machine polled from the GTK main loop: each tick either waits for the
window to settle or performs one numbered step, appending a line to ``checks`` for every behaviour
it has confirmed. ``controls_only`` runs the shorter pane-control branch (steps 35 to 52).
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from workspace.app import Gdk, Gio, GLib, Gtk, Pango, Vte, Window, widget_children
from workspace.cli import Application
from workspace.gallery import geometry
from workspace.model import State

from .fakes import FakeAgents
from .fixtures import build


def run_smoke(args, controls_only=False, capture_only=False):
    """Run the scenario and return the process exit status."""
    with tempfile.TemporaryDirectory(prefix="workspace-native-fixture-") as directory:
        fixtures = build(Path(directory))
        root, project, child, output, bulk, slow = (fixtures.root, fixtures.project, fixtures.child,
                                                    fixtures.output, fixtures.bulk, fixtures.slow)
        args.left, args.right = str(child), str(output)
        args.state = root / "state"
        app = Application(args, unique=False)
        fake = FakeAgents()
        app.client = fake
        checks = []
        step = {"number": 0, "ticks": 0}
        slow_continue = threading.Event()

        def screenshot(name, retry=0):
            from gi.repository import Gsk, Graphene
            window = app.window
            width, height = window.get_child().get_width(), window.get_child().get_height()
            snapshot = Gtk.Snapshot.new()
            paintable = window.smoke_paintable
            paintable.snapshot(snapshot, width, height)
            node = snapshot.to_node()
            if node is None:
                if retry < 10:
                    window.get_child().queue_draw()
                    GLib.timeout_add(50, lambda: (screenshot(name, retry + 1), False)[1])
                return None
            renderer = Gsk.Renderer.new_for_surface(window.get_surface())
            assert renderer is not None
            bounds = Graphene.Rect().init(0, 0, width, height)
            texture = renderer.render_texture(node, bounds)
            renderer.unrealize()
            texture.save_to_png("/tmp/workspace-" + name + ".png")
            downloader = Gdk.TextureDownloader.new(texture)
            downloader.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED)
            pixels, stride = downloader.download_bytes()

            class Pixels:
                def get_stride(self):
                    return stride

                def get_data(self):
                    return pixels.get_data()

            return Pixels()

        def run():
            try:
                step["ticks"] += 1
                if step["ticks"] > 180:
                    current = app.window.panes[0] if app.window else None
                    raise AssertionError(f"Native smoke timed out at step {step['number']}: "
                                         f"path={getattr(current, 'path', None)} "
                                         f"count={current.count.get_text() if current else None} "
                                         f"cursor={current.view.get_cursor() if current else None} "
                                         f"focus={app.window.get_focus() if app.window else None} "
                                         f"right_entries={len(app.window.panes[1].entries) if app.window and app.window.panes else None} "
                                         f"geometry={current.geometry_measured if current else None} "
                                         f"capacity={(current.capacity, current.visible_capacity()) if current else None} "
                                         f"allocations={(current.view.get_allocated_width(), current.view.get_allocated_height()) if current else None} "
                                         f"stack={current.content_stack.get_visible_child_name() if current else None} "
                                         f"scroll={current.scroll.get_vadjustment().get_value() if current else None} "
                                         f"selected={current.selected_paths() if current else None} "
                                         f"gallery={(current.gallery.columns, current.gallery.capacity, current.gallery.layout(), current.gallery.resize_source, current.gallery.building, len(current.gallery.children_by_logical)) if current and current.gallery.flow else None} "
                                         f"gallery_scroll={(current.gallery.scroll.get_vadjustment().get_upper(), current.gallery.scroll.get_vadjustment().get_page_size(), current.gallery.scroll.get_vadjustment().get_value(), len(widget_children(current.gallery.flow)), [(item.gallery_row, item.get_allocation().y) for item in current.gallery.flow.get_selected_children()]) if current and current.gallery.flow else None} "
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
                    assert len(window.shells.terminals) == 1
                    assert len(window.shells.tab_bar.get_children()) == 1
                    assert not window.shells.notebook.get_show_tabs()
                    assert window.shells.terminals[0][0].workspace_tab_button.has_css_class("top-tab-active")
                    assert window.file_panes is None and not window.panes
                    assert window.gallery_flow is None
                    assert not hasattr(window, "regime_button")
                    assert window.shells.terminals[0][0].workspace_tab_button.get_label() == str(child)
                    assert window.shells.terminals[0][0].workspace_tab_button.get_child().get_max_width_chars() == 24
                    terminal_font = window.shells.terminals[0][0].get_font()
                    assert terminal_font.get_size_is_absolute()
                    assert terminal_font.get_size() == window.appearance["fonts"]["files_px"] * Pango.SCALE
                    assert terminal_font.get_weight() == window.appearance["fonts"]["files_weight"]
                    checks.append("shell startup leaves Files and Gallery unloaded")
                    screenshot("terminal-start")
                    assert window.get_focus() is window.shells.terminals[0][0]
                    assert window.shells.terminal_shortcut(window.shells.terminals[0][0],
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.file_panes is not None and len(window.panes) == 2
                    assert window.gallery_flow is None
                    expected_first_width = max(120, (window.stack.get_width() // 2 - 40) // 2)
                    assert all(pane.view.get_item_width() >= expected_first_width
                               for pane in window.panes)
                    step["startup_checked"] = True
                    return True
                if step["number"] == 0 and (not window.context or len(window.panes[0].store) < 5
                                            or not window.panes[1].entries
                                            or window.panes[0].folder_path.get_text() != str(child)
                                            or window.get_focus() is not window.panes[0].view
                                            or any(" free" not in pane.folder_stats.get_text()
                                                   for pane in window.panes)
                                            or not window.panes[0].geometry_measured
                                            or window.panes[0].view.get_cell_rect(
                                                Gtk.TreePath.new_from_indices([0]), None)[1].y < 0
                                            or window.panes[0].capacity != window.panes[0].visible_capacity()
                                            or window.file_panes.get_width() < 2
                                            or abs(window.file_panes.get_position() /
                                                   window.file_panes.get_width() - .5) >= .02):
                    return True
                if capture_only and step["number"] == 0:
                    if screenshot("commander") is None:
                        return True
                    assert all(pane.view.get_tooltip_column() == -1 and not pane.view.get_has_tooltip()
                               for pane in window.panes)
                    assert window.panes[0].sort_button.get_tooltip_text()
                    right = window.panes[1]
                    placeholder = Gtk.TreePath.new_from_indices([1])
                    assert not right.store[placeholder][3]
                    right.view.unselect_all()
                    right.view.set_cursor(placeholder, None, False)
                    right.view.select_path(placeholder)
                    right.selected()
                    real = Gtk.TreePath.new_from_indices([0])
                    assert right.cursor_logical() == 0 and right.view.path_is_selected(real)
                    assert not right.view.path_is_selected(placeholder)
                    step["number"] = 53
                    app.quit()
                    return False
                if controls_only and step["number"] == 0:
                    pane = window.panes[0]
                    assert window.workspace_button.get_label() == "Commander ▾"
                    assert pane.sort_button.get_label() == "n"
                    assert pane.view_button.get_icon_name() == "view-list-symbolic"
                    assert pane.sort_button.get_next_sibling() is pane.view_button
                    assert pane.view_button.get_next_sibling() is pane.location_bar
                    assert pane.location_bar.get_child() is pane.location
                    assert pane.locations_button.get_parent() is pane.location_bar
                    assert pane.find_button.get_parent() is pane.location_bar
                    assert isinstance(pane.find_button.get_child(), Gtk.Image)
                    assert "Alt+F7" in pane.find_button.get_tooltip_text()
                    assert pane.search.get_placeholder_text() == "Go to filename in this folder"
                    star_before = pane.locations_button.get_icon_name()
                    pane.favourite()
                    assert pane.locations_button.get_icon_name() != star_before
                    pane.favourite()
                    assert pane.locations_button.get_icon_name() == star_before
                    pane.set_sort("size")
                    assert pane.sort_button.get_label() == "s"
                    pane.set_sort("size")
                    assert pane.sort_button.get_label() == "S"
                    pane.set_sort("name")
                    assert pane.sort_button.get_label() == "n"
                    screenshot("files-controls")
                    pane.sort_button.emit("clicked")
                    step["number"] = 35
                    return True
                if controls_only and step["number"] == 35:
                    pane = window.panes[0]
                    sort_menu = pane.sort_button.nightty_popover
                    assert sort_menu.nightty_category == "sort"
                    assert sort_menu.get_parent() is pane.sort_button
                    assert sort_menu.get_has_arrow()
                    assert sort_menu.get_child().get_first_child() is sort_menu.nightty_scroller
                    assert window.get_focus() is sort_menu.nightty_items[0]
                    sort_menu.popdown()
                    window.focus_active_browser()
                    window.show_workspace_menu()
                    assert any("Project" in item.get_label()
                               for item in window.workspace_button.nightty_popover.nightty_items)
                    window.workspace_button.nightty_popover.popdown()
                    window.state.save(favourites=[str(child / f"bookmark-{index:02d}")
                                                  for index in range(28)])
                    gvfs_root = f"/run/user/{os.getuid()}/gvfs"
                    native_is_dir, native_scandir = Path.is_dir, os.scandir

                    def no_gvfs_stat(path, *args, **kwargs):
                        assert os.fspath(path) != gvfs_root, "popup must not stat GVfs on the UI thread"
                        return native_is_dir(path, *args, **kwargs)

                    def no_gvfs_scan(path):
                        assert os.fspath(path) != gvfs_root, "popup must not scan GVfs on the UI thread"
                        return native_scandir(path)

                    with patch.object(Path, "is_dir", no_gvfs_stat), patch.object(os, "scandir", no_gvfs_scan):
                        assert window.key(window, argparse.Namespace(
                            keyval=Gdk.KEY_F1, state=Gdk.ModifierType.CONTROL_MASK))
                        first_menu = pane.locations_button.nightty_popover
                        assert window.key(window, argparse.Namespace(
                            keyval=Gdk.KEY_F1, state=Gdk.ModifierType.CONTROL_MASK))
                        assert first_menu.get_parent() is None
                    menu = pane.locations_button.nightty_popover
                    assert menu.nightty_category == "location"
                    assert any(item.get_label() == "Home" for item in menu.nightty_items)
                    assert any("current folder" in item.get_label() for item in menu.nightty_items)
                    assert isinstance(menu.nightty_scroller.get_child(), Gtk.Viewport)
                    step["number"] = 36
                    return True
                if controls_only and step["number"] == 36:
                    pane = window.panes[0]
                    menu = pane.locations_button.nightty_popover
                    for _ in menu.nightty_items:
                        assert window.pane_menu_key(pane, menu, pane.locations_button, menu,
                            argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                        assert window.get_focus() in menu.nightty_items
                    assert not pane.view.has_focus()
                    step["number"] = 37
                    return True
                if controls_only and step["number"] == 37:
                    pane = window.panes[0]
                    menu = pane.locations_button.nightty_popover
                    adjustment = menu.nightty_scroller.get_vadjustment()
                    assert adjustment.get_value() > 0, (adjustment.get_upper(),
                        adjustment.get_page_size(), adjustment.get_value(),
                        window.get_focus(), menu.nightty_scroller.get_allocated_height())
                    focused = window.get_focus()
                    position = focused.translate_coordinates(menu.nightty_content, 0, 0)
                    focused_top = position[-1]
                    assert adjustment.get_value() <= focused_top
                    assert focused_top + focused.get_allocated_height() <= (
                        adjustment.get_value() + adjustment.get_page_size())
                    assert window.pane_menu_key(pane, menu, pane.locations_button, menu,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    step["number"] = 38
                    return True
                if controls_only and step["number"] == 38:
                    pane = window.panes[0]
                    menu = pane.view_button.nightty_popover
                    assert menu.nightty_category == "view"
                    assert menu.get_parent() is pane.view_button and menu.get_has_arrow()
                    assert window.get_focus() is menu.nightty_items[0]
                    assert any("Gallery" in item.get_label() for item in menu.nightty_items)
                    assert window.pane_menu_key(pane, menu, pane.view_button, menu,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    step["number"] = 39
                    return True
                if controls_only and step["number"] == 39:
                    pane = window.panes[0]
                    menu = pane.sort_button.nightty_popover
                    assert menu.nightty_category == "sort" and menu.get_parent() is pane.sort_button
                    assert window.pane_menu_key(pane, menu, pane.sort_button, menu,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    step["number"] = 40
                    return True
                if controls_only and step["number"] == 40:
                    pane = window.panes[0]
                    menu = pane.view_button.nightty_popover
                    assert menu.nightty_category == "view" and menu.get_parent() is pane.view_button
                    assert window.pane_menu_key(pane, menu, pane.view_button, menu,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    step["number"] = 41
                    return True
                if controls_only and step["number"] == 41:
                    pane = window.panes[0]
                    menu = pane.locations_button.nightty_popover
                    assert menu.nightty_category == "location"
                    assert menu.get_parent() is pane.locations_button and menu.get_has_arrow()
                    checks.append("visible mode, location, view, and reversible sort controls")
                    checks.append("location choices retain visible focus while moving down")
                    checks.append("popup keys follow Sort, View, Favourites order")
                    menu.popdown()
                    pane.focus_file_path(child / "draft.md")
                    dialog = window.operations.copy()
                    assert isinstance(dialog, Gtk.Window)
                    step["copy_dialog"] = dialog
                    step["number"] = 42
                    return True
                if controls_only and step["number"] == 42:
                    dialog = step["copy_dialog"]
                    choices = widget_children(dialog.get_child().get_last_child())
                    assert [choice.get_label() for choice in choices] == ["_Yes", "_Background", "_Cancel"]
                    assert dialog.get_default_widget() is choices[0]
                    assert dialog.get_focus() is choices[0]
                    assert all(choice.get_receives_default() for choice in choices)
                    assert choices[0].has_css_class("mc-dialog-focused")
                    choices[1].grab_focus()
                    step["number"] = 43
                    return True
                if controls_only and step["number"] == 43:
                    dialog = step["copy_dialog"]
                    choices = widget_children(dialog.get_child().get_last_child())
                    assert dialog.get_focus() is choices[1]
                    assert not choices[0].has_css_class("mc-dialog-focused")
                    assert choices[1].has_css_class("mc-dialog-focused")
                    dialog.destroy()
                    checks.append("copy dialog focus fill follows the selected action")
                    step["same_folder_dialog"] = window.operations.copy(same_folder=True)
                    step["number"] = 44
                    return True
                if controls_only and step["number"] in {44, 45}:
                    dialog = step["same_folder_dialog"]
                    fields = widget_children(dialog.get_child())[1]
                    destination = widget_children(fields)[3]
                    source = child / "draft.md"
                    assert destination.get_text() == str(source)
                    focus = dialog.get_focus()
                    assert focus is destination or focus.get_parent() is destination
                    assert destination.get_selection_bounds()[-2:] == (
                        len(str(source.parent)) + 1, len(str(source)))
                    if step["number"] == 45:
                        destination.set_text("")
                        destination.emit("activate")
                        error = widget_children(dialog.get_child())[3]
                        assert error.get_visible() and error.get_text() == "Enter a destination."
                    dialog.destroy()
                    if step["number"] == 44:
                        step["same_folder_dialog"] = window.operations.move(same_folder=True)
                        step["number"] = 45
                        return True
                    checks.append("same-folder copy and rename focus the selected destination filename; Enter runs Yes")
                    job = window.operations.start_transfer([child / "draft.md"], output, background=False)
                    step["foreground_job"] = job
                    step["foreground_dialog"] = window.operations.show_transfer_progress(job)
                    step["number"] = 46
                    return True
                if controls_only and step["number"] == 46:
                    job = step["foreground_job"]
                    assert job.status in {"queued", "copying", "completed"}, (job.status, job.error)
                    if job.status != "completed" or step["foreground_dialog"].get_visible():
                        return True
                    assert (output / "draft.md").read_bytes() == (child / "draft.md").read_bytes()
                    checks.append("successful foreground copy closes its progress dialog")
                    job = window.operations.start_transfer([child / "draft.md"], output, background=False)
                    step["foreground_job"] = job
                    step["foreground_dialog"] = window.operations.show_transfer_progress(job)
                    step["number"] = 47
                    return True
                if controls_only and step["number"] == 47:
                    job = step["foreground_job"]
                    if job.status in {"queued", "copying"} or getattr(job, "conflict_dialog", None) is None:
                        return True
                    assert job.status == "conflict" and job.conflict is not None
                    conflict_dialog = job.conflict_dialog
                    rows = widget_children(conflict_dialog.get_child())[2:]
                    choices = [choice for row in rows for choice in widget_children(row)]
                    assert [choice.get_label() for choice in choices] == [
                        "_Yes", "_No", "_Abort", "_All", "N_one", "Size _differs",
                        "If _older", "If _smaller"]
                    assert conflict_dialog.get_default_widget() is choices[0]
                    choices[1].emit("clicked")
                    step["number"] = 48
                    return True
                if controls_only and step["number"] == 48:
                    job = step["foreground_job"]
                    if job.status != "completed" or step["foreground_dialog"].get_visible():
                        return True
                    assert job.skipped == 1
                    assert (output / "draft.md").read_bytes() == (child / "draft.md").read_bytes()
                    checks.append("foreground conflict offers MC overwrite choices and No skips one file")
                    step["background_job"] = window.operations.start_transfer(
                        [child / "draft.md"], output, background=True)
                    step["number"] = 49
                    return True
                if controls_only and step["number"] == 49:
                    job = step["background_job"]
                    if job.status in {"queued", "copying"}:
                        return True
                    assert job.status == "conflict" and job.conflict is not None
                    window.operations.update()
                    assert window.operations.button.has_css_class("transfer-alert")
                    window.operations.show_jobs()
                    choices = [choice for row in widget_children(job.conflict_dialog.get_child())[2:]
                               for choice in widget_children(row)]
                    choices[5].emit("clicked")
                    step["number"] = 50
                    return True
                if controls_only and step["number"] == 50:
                    job = step["background_job"]
                    if job.status != "completed":
                        return True
                    assert job.skipped == 1
                    window.operations.update()
                    assert not window.operations.button.has_css_class("transfer-alert")
                    checks.append("background conflict alerts in Transfers and Size differs skips equal files")
                    pane = window.panes[0]
                    pane.navigate(child)
                    while pane.path != Path("/"):
                        pane.up()
                    step["number"] = 51
                    return True
                if controls_only and step["number"] == 51:
                    pane = window.panes[0]
                    if (pane.path != Path("/") or "loading" in pane.count.get_text().lower()
                            or pane.model_fill_source or not pane.geometry_measured):
                        return True
                    target_index = min(pane.capacity - 1, len(pane.entries) - 1)
                    assert target_index > 4
                    target = Path(pane.entries[target_index][3])
                    pane.focus_file_path(target)
                    step["number"] = 52
                    return True
                if controls_only and step["number"] == 52:
                    pane = window.panes[0]
                    cursor = pane.view.get_cursor()[1]
                    assert cursor is not None and pane.view.path_is_selected(cursor)
                    found, rect = pane.view.get_cell_rect(cursor, None)
                    assert found
                    row_y = rect.y + pane.item_pitch() // 2 - pane.scroll.get_vadjustment().get_value()
                    assert row_y < pane.column_visible_bottom(
                        pane.scroll_overlay.drawing_area.get_allocated_height())
                    surface = screenshot("rapid-up-root")
                    assert surface is not None
                    x, y = pane.view.translate_coordinates(window.get_child(),
                        rect.x + pane.view.get_item_width() // 2, rect.y + pane.item_pitch() // 2)
                    pixel = bytes(surface.get_data())[(int(y) * surface.get_stride() + int(x) * 4):
                                                       (int(y) * surface.get_stride() + int(x) * 4 + 4)]
                    accent = window.palette["accent"]
                    assert pixel[:3] == bytes.fromhex(accent[5:7] + accent[3:5] + accent[1:3]), pixel
                    checks.append("rapid Up navigation to root keeps lower rows and cursor visible")
                    app.quit()
                    return False
                if step["number"] == 0 and not step.get("footer_checked"):
                    if not step.get("footer_check_started"):
                        step["footer_before"] = (window.panes[0].status_box.get_height(),
                                                 window.panes[0].scroll.get_height(),
                                                 window.panes[0].capacity)
                        window.panes[0].status.set_text("搭乗用バーコード登録 - JAL国内線予約 (1)")
                        step["footer_check_started"] = True
                        return True
                    left, right = window.panes
                    left_y = left.list_end.translate_coordinates(window.file_panes, 0, 0)[1]
                    right_y = right.list_end.translate_coordinates(window.file_panes, 0, 0)[1]
                    assert left_y == right_y, (left_y, right_y,
                        left.status_box.get_allocated_height(), right.status_box.get_allocated_height())
                    assert (left.status_box.get_height(), left.scroll.get_height(), left.capacity) == step["footer_before"]
                    screenshot("footer-japanese")
                    window.refresh_file_footer(left)
                    step["footer_checked"] = True
                    checks.append("Japanese footer text keeps height and both pane bottom lines stable")
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
                    assert window.capture_browser_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1]
                    assert window.status is window.panes[1].status
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_browser_key(None, Gdk.KEY_ISO_Left_Tab, 0,
                        Gdk.ModifierType.SHIFT_MASK)
                    assert window.active is window.panes[0]
                    assert window.get_focus() is window.panes[0].view
                    window.operations.button.grab_focus()
                    assert window.capture_browser_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1]
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_browser_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
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
                    assert pane.store[4][1] == "0-日本語.txt"
                    row_rects = []
                    for row in range(pane.capacity + 1):
                        found, rect = pane.view.get_cell_rect(
                            Gtk.TreePath.new_from_indices([2 * row]), None)
                        assert found
                        row_rects.append(rect)
                    assert all(rect.height == row_rects[0].height
                               and rect.y == row_rects[0].y + row * pane.item_pitch()
                               for row, rect in enumerate(row_rects)), (
                                   pane.item_pitch(), pane.capacity, pane.view.get_item_width(),
                                   [(rect.y, rect.height) for rect in row_rects])
                    viewport = pane.scroll.get_vadjustment().get_page_size()
                    assert row_rects[-2].y + pane.item_pitch() <= viewport
                    assert row_rects[-1].y + pane.item_pitch() > viewport
                    japanese_right = next(i for i, entry in enumerate(pane.entries)
                                          if entry[1] == "note-16-日本語.txt")
                    assert pane.capacity <= japanese_right < 2 * pane.capacity
                    left_partner = pane.store_position(japanese_right - pane.capacity, 0)
                    left_path = Gtk.TreePath.new_from_indices([left_partner])
                    pane.view.unselect_all()
                    pane.view.set_cursor(left_path, None, False)
                    pane.view.select_path(left_path)
                    footer_height = pane.status_box.get_height()
                    assert pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert pane.cursor_logical() == japanese_right
                    assert len(pane.view.get_selected_items()) == 1
                    assert pane.selected_paths() == [child / "note-16-日本語.txt"]
                    assert pane.status_box.get_height() == footer_height
                    checks.append("Japanese filenames keep row height and one right-column cursor")
                    pane.view.unselect_all()
                    step["capacity"] = pane.capacity
                    left_bottom = pane.store_position(pane.capacity - 1, 0)
                    pane.view.set_cursor(Gtk.TreePath.new_from_indices([left_bottom]), None, False)
                    pane.move_by_key(pane.view, argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    assert pane.cursor_logical() == pane.capacity
                    cursor_path = pane.view.get_cursor()[1]
                    found, cursor_rect = pane.view.get_cell_rect(cursor_path, None)
                    assert found and cursor_path.get_indices()[0] % 2 == 1
                    assert cursor_rect.y + cursor_rect.height <= viewport
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
                    window.operations.button.grab_focus()
                    assert window.browser_arrow_from_control(window.operations.button,
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
                    assert [i for i in positions if pane.view.path_is_selected(
                        Gtk.TreePath.new_from_indices([i]))] == [positions[0]]
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
                    window.operations.button.grab_focus()
                    assert window.capture_browser_key(None, Gdk.KEY_End, 0, Gdk.ModifierType(0))
                    assert pane.cursor_logical() == len(pane.entries) - 1
                    assert window.capture_browser_key(None, Gdk.KEY_Home, 0, Gdk.ModifierType(0))
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
                    checks.append("Alt+S quick goto finds a filename in the active pane")
                    window.add_favourite()
                    assert child.resolve() in window.state.favourites
                    locations = window.build_locations_menu(window.panes[0])
                    assert any(item.get_label() == str(child.resolve())
                               for item in locations.nightty_items)
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
                    if (window.quick_image.get_paintable() is None
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
                    window.queue_resize()
                    pane = window.active
                    if pane.gallery.resize_source:
                        return True
                    if pane.gallery.layout() != (pane.gallery.columns, pane.gallery.capacity):
                        pane.gallery.apply_resize()
                        return True
                    matches = [item for item in window.active.gallery.children_by_logical
                               if item is not None and item.file_path.name == "visual.png"]
                    pdf_cards = [item for item in window.active.gallery.children_by_logical
                                 if item is not None and item.file_path.name == "sample.pdf"]
                    if (not matches or matches[0].preview_image.get_paintable() is None
                            or not pdf_cards or pdf_cards[0].preview_image.get_paintable() is None):
                        return True
                    assert pane.gallery.columns == 2
                    assert pdf_cards[0].preview_image.get_paintable().get_intrinsic_height() <= 80
                    if widget_children(pane.gallery.flow)[0].get_allocated_height() < 108:
                        return True
                    assert widget_children(pane.gallery.flow)[0].get_allocated_height() == 108, (
                        widget_children(pane.gallery.flow)[0].get_allocated_height(),
                        pdf_cards[0].get_allocated_height())
                    if not step.get("gallery_entry_rebuild_started"):
                        assert pane.gallery.serial == 1
                        assert window.get_focus() is pane.gallery.flow.get_focus_child()
                        checks.append("gallery first layout uses the pane width and retains focus")
                        window.command_entry.grab_focus()
                        pane.gallery.show()
                        step["gallery_entry_rebuild_started"] = True
                        return True
                    if pane.gallery.building or pane.gallery.resize_source:
                        return True
                    assert window.get_focus().get_ancestor(Gtk.Entry) is window.command_entry
                    window.focus_active_browser()
                    checks.append("gallery rebuild leaves command entry focus intact")
                    parent_tile = widget_children(window.gallery_flow)[0]
                    assert parent_tile.is_parent and isinstance(parent_tile.get_child(), Gtk.Box)
                    assert parent_tile.get_child().get_children()[0].get_text() == ".."
                    parent_label = parent_tile.get_child().get_children()[0]
                    window.queue_resize()
                    before = parent_label.translate_coordinates(window.gallery_flow, 0, 0)
                    window.gallery_flow.select_child(parent_tile)
                    window.queue_resize()
                    assert parent_label.translate_coordinates(window.gallery_flow, 0, 0) == before, (
                        before, parent_label.translate_coordinates(window.gallery_flow, 0, 0))
                    parent_tile.grab_focus()
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert (parent_tile in window.gallery_flow.get_selected_children()
                            and all(item.gallery_index == 0 for item in window.gallery_flow.get_selected_children())), (
                                [(item.gallery_index, item.gallery_row) for item in window.gallery_flow.get_selected_children()],
                                window.active.gallery.top(), window.gallery_flow.get_focus_child())
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert window.gallery_flow.get_selected_children()[0] is window.active.gallery.children_by_logical[
                        window.active.gallery.capacity]
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    assert (parent_tile in window.gallery_flow.get_selected_children()
                            and all(item.gallery_index == 0 for item in window.gallery_flow.get_selected_children()))
                    assert window.capture_browser_key(None, Gdk.KEY_End, 0, Gdk.ModifierType(0))
                    assert window.active.gallery.selected_paths() == [child / "zz-track.mp3"]
                    assert window.capture_browser_key(None, Gdk.KEY_Home, 0, Gdk.ModifierType(0))
                    assert (parent_tile in window.gallery_flow.get_selected_children()
                            and all(item.gallery_index == 0 for item in window.gallery_flow.get_selected_children())), (
                                [(item.gallery_index, item.gallery_row) for item in window.gallery_flow.get_selected_children()],
                                window.active.gallery.top(), window.gallery_flow.get_focus_child())
                    checks.append("Home and End reach both ends of the gallery")
                    window.gallery_flow.unselect_all()
                    audio_tile = next(item for item in window.active.gallery.children_by_logical
                                      if item is not None and item.file_path.name == "zz-track.mp3")
                    if audio_tile.preview_image.get_storage_type() != Gtk.ImageType.GICON:
                        return True
                    assert audio_tile.preview_image.get_gicon().equal(
                        Gio.content_type_get_icon(Gio.content_type_guess(str(child / "zz-track.mp3"), None)[0]))
                    matches[0].gallery_pane.gallery.scroll_to(matches[0])
                    if window.active.gallery.scroll.get_vadjustment().get_value() <= 0:
                        return True
                    visible_match = window.active.gallery.child_for_index(matches[0].gallery_index, window.active.gallery.top())
                    assert window.active.gallery.click(visible_match,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), visible_match)
                    assert window.active.gallery.selected_paths() == [child / "visual.png"]
                    assert window.active.selected_paths() == [child / "visual.png"]
                    visible_match.grab_focus()
                    assert window.stack.get_visible_child_name() == "files"
                    assert window.active.content_stack.get_visible_child_name() == "gallery"
                    assert window.panes[1].content_stack.get_visible_child_name() == "files"
                    assert matches[0].get_allocated_width() > matches[0].get_allocated_height()
                    checks.append("gallery uses media previews, file type icons and a stable parent card")
                    window.command_entry.grab_focus()
                    assert window.capture_browser_key(None, Gdk.KEY_Tab, 0, Gdk.ModifierType(0))
                    assert window.active is window.panes[1] and window.mode == "files"
                    assert window.get_focus() is window.panes[1].view
                    assert window.capture_browser_key(None, Gdk.KEY_ISO_Left_Tab, 0,
                        Gdk.ModifierType.SHIFT_MASK)
                    assert window.active is window.panes[0] and window.mode == "gallery"
                    focus = window.get_focus()
                    assert focus is window.gallery_flow or focus.is_ancestor(window.gallery_flow)
                    checks.append("Tab keeps focus in mixed Gallery and Files panes")
                    window.active.gallery.scroll.get_vadjustment().set_value(0)
                    step["number"] = 2.5
                    return True
                elif number == 2.5:
                    gallery_pane = window.active
                    matches = [item for item in gallery_pane.gallery.children_by_logical
                               if item is not None and item.file_path.name == "visual.png"]
                    surface = screenshot("gallery-top")
                    first_partial = gallery_pane.gallery.child_for_index(gallery_pane.gallery.capacity, gallery_pane.gallery.capacity)
                    assert first_partial.gallery_row == gallery_pane.gallery.capacity
                    covered_x = int(first_partial.translate_coordinates(window, 20, 20)[0])
                    sample_y = min(gallery_pane.gallery.scroll.get_height() - 10,
                                   gallery_pane.gallery.visible_bottom() + 5)
                    covered_y = int(gallery_pane.gallery.scroll.translate_coordinates(
                        window.get_child(), 0, sample_y)[1])
                    pixel = bytes(surface.get_data())[(covered_y * surface.get_stride() + covered_x * 4):
                                                      (covered_y * surface.get_stride() + covered_x * 4 + 4)]
                    backgrounds = [window.palette[key] for key in ("background", "surface")]
                    assert pixel[:3] in [bytes.fromhex(color[5:7] + color[3:5] + color[1:3])
                                         for color in backgrounds], pixel
                    assert gallery_pane.gallery.click(first_partial,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), first_partial)
                    assert all(item.gallery_row < gallery_pane.gallery.capacity
                               for item in gallery_pane.gallery.flow.get_selected_children())
                    _columns, pitch, _first_y = geometry(
                        gallery_pane.gallery.flow, widget_children(gallery_pane.gallery.flow))
                    gallery_pane.gallery.scroll.get_vadjustment().set_value(3 * pitch)
                    gallery_pane.gallery.snap_scroll()
                    assert gallery_pane.gallery.top() == 3
                    for row in range(3, 3 + gallery_pane.gallery.capacity):
                        visible = [item.gallery_index for item in widget_children(gallery_pane.gallery.flow)
                                   if getattr(item, "gallery_row", -1) == row]
                        assert visible == [row, row + gallery_pane.gallery.capacity], (row, visible)
                    duplicate = gallery_pane.gallery.child_for_index(gallery_pane.gallery.capacity + 1, 3)
                    assert duplicate.gallery_row == gallery_pane.gallery.capacity + 1
                    assert gallery_pane.gallery.click(duplicate,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=20, y=20), duplicate)
                    assert gallery_pane.gallery.top() == 3
                    assert window.active.gallery.selected_paths() == [duplicate.file_path]
                    checks.append("gallery scrolling keeps consecutive columns and masks partial cards")
                    matches[0].gallery_pane.gallery.scroll_to(matches[0])
                    screenshot("gallery")
                    logical_children = window.active.gallery.children_by_logical
                    capacity = window.active.gallery.capacity
                    last_top = window.active.gallery.max_top()
                    left_bottom = window.active.gallery.child_for_index(min(last_top + capacity, len(logical_children)) - 1, last_top)
                    last_right = window.active.gallery.child_for_index(len(logical_children) - 1, last_top)
                    assert left_bottom.get_allocation().x < last_right.get_allocation().x, (
                        len(logical_children), capacity, window.active.gallery.columns,
                        left_bottom.gallery_index, left_bottom.get_allocation().x,
                        last_right.gallery_index, last_right.get_allocation().x)
                    window.gallery_flow.unselect_all()
                    window.gallery_flow.select_child(left_bottom)
                    left_bottom.grab_focus()
                    assert window.capture_browser_key(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))
                    assert window.active.gallery.selected_paths() == [child / "zz-track.mp3"]
                    assert window.active is window.panes[0]
                    window.gallery_flow.unselect_all()
                    window.gallery_flow.select_child(left_bottom)
                    left_bottom.grab_focus()
                    window.operations.button.grab_focus()
                    assert window.browser_arrow_from_control(window.operations.button,
                        argparse.Namespace(keyval=Gdk.KEY_Right, state=0))
                    assert window.active.gallery.selected_paths() == [child / "zz-track.mp3"]
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
                        window.active.gallery.activated(window.gallery_flow, matches[0])
                        file_row = next(i for i, row in enumerate(window.panes[0].store)
                                        if row[1] == "visual.png")
                        window.panes[0].open(window.panes[0].view,
                            Gtk.TreePath.new_from_indices([file_row]))
                    finally:
                        window.open_external = original_open_external
                    assert opened == [child / "visual.png", child / "visual.png"]
                    assert window.mode == "gallery"
                    checks.append("Enter opens the default application in Files and Gallery")
                    window.job = window.operations.start_copy(window.active.gallery.selected_paths(), output)
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
                    assert window.active.gallery.selected_paths() == [child / "visual.png"]
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
                    assert window.image.get_paintable() is not None
                    checks.append("PDF first-page preview")
                    screenshot("inspect-pdf")
                    window.set_mode("project")
                    screenshot("project")
                    window.shells.new(command=["/bin/sh", "-c", "printf 'SURFACE_TERMINAL_OK\\n'; exec /bin/sh"])
                    window.set_mode("console")
                    step["number"] = 6
                elif number == 6:
                    terminal = window.shells.terminals[-1][0]
                    text = terminal.get_text_format(Vte.Format.TEXT) or ""
                    if "SURFACE_TERMINAL_OK" not in [line.strip() for line in text.splitlines()]:
                        return True
                    shell_tab = window.shells.terminals[1][0]
                    shell_tab.workspace_browser["paths"] = (bulk, output)
                    generation = tuple(pane.generation for pane in window.panes)
                    window.shells.select(0)
                    window.shells.select(1)
                    assert tuple(pane.generation for pane in window.panes) == generation
                    assert window.panes[0].path == child
                    shell_tab.workspace_browser["paths"] = (child, output)
                    checks.append("shell tabs do not scan unopened file views")
                    window.hide()
                    assert any(pid for _terminal, pid in window.shells.terminals)
                    app.activate()
                    assert window.get_visible()
                    checks.extend(["embedded terminal output", "hide preserves process"])
                    screenshot("console")
                    assert len(window.shells.terminals) == 2
                    assert window.shells.top_tab_shortcut("Page_Up", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "project" and window.shells.notebook.get_current_page() == 0
                    assert window.shells.top_tab_shortcut("Page_Down", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "console" and window.shells.notebook.get_current_page() == 1
                    assert window.shells.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_2, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "project"
                    assert window.shells.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_3, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    checks.append("idle shell shortcuts open native views directly")
                    window.set_mode("files")
                    assert window.shells.top_tab_shortcut("Page_Up", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "project"
                    assert window.shells.top_tab_shortcut("Page_Down", Gdk.ModifierType.CONTROL_MASK)
                    assert window.mode == "files" and window.shells.notebook.get_current_page() == 1
                    checks.append("Ctrl+PageUp and Ctrl+PageDown retain each tab's view")
                    window.operations.show_jobs()
                    assert window.operations.popover.get_visible()
                    assert "COMPLETED" in window.operations.jobs_label.get_text()
                    window.operations.show_jobs()
                    checks.append("transfers open in a compact dropdown")
                    window.set_mode("project")
                    window.project_tabs.set_current_page(2)
                    step["number"] = 7
                elif number == 7:
                    if window.agent_panel.profile_picker.get_active_id() != "fixture":
                        return True
                    assert not fake.calls
                    window.agent_panel.compose.get_buffer().set_text("Inspect this fixture")
                    window.agent_panel.send()
                    step["number"] = 8
                elif number == 8:
                    if window.agent_panel.sending:
                        return True
                    # Refresh can replace the composer buffer; the retry must
                    # still keep its previously created session and request ID.
                    window.agent_panel.refresh()
                    step["number"] = 9
                elif number == 9:
                    if window.agent_panel.loading or window.agent_panel.profile_picker.get_active_id() != "fixture":
                        return True
                    window.agent_panel.send()
                    step["number"] = 10
                elif number == 10:
                    text = window.agent_panel.chat.get_buffer().get_text(window.agent_panel.chat.get_buffer().get_start_iter(), window.agent_panel.chat.get_buffer().get_end_iter(), False)
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
                    window.panes[0].view.grab_focus()
                    window.command_entry.set_text("abλ")
                    for expected in ("ab", "a", ""):
                        assert window.panes[0].move_by_key(window.panes[0].view,
                            argparse.Namespace(keyval=Gdk.KEY_BackSpace, state=0))
                        assert window.command_entry.get_text() == expected
                        assert window.get_focus() is window.panes[0].view
                    checks.append("Backspace edits the command line while file pane focus stays put")
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
                    assert window.shells.terminal_shortcut(terminal,
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
                    assert window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_p, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "project"
                    assert window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_p, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    assert window.shells.terminal_shortcut(terminal,
                        argparse.Namespace(keyval=Gdk.KEY_p,
                                           state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
                    terminal.feed_child(b"\x15")
                    checks.append("idle shell Ctrl+P opens Project; Ctrl+Shift+P sends shell history key")
                    window.set_mode("project")
                    assert window.mode == "project"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.get_visible() and window.mode == "project"
                    window.mode_shortcut("p")
                    assert window.mode == "console"
                    window.set_mode("files")
                    assert window.mode_shortcut("p") and window.mode == "project"
                    assert window.mode_shortcut("p") and window.mode == "files"
                    assert window.get_focus() is window.active.view
                    window.mode_shortcut("p")
                    window.mode_shortcut("o")
                    assert window.mode == "console"
                    window.mode_shortcut("o")
                    assert window.mode == "project"
                    window.mode_shortcut("p")
                    assert window.mode == "files"
                    checks.append("Ctrl+P returns to Commander after Project and Ctrl+O shell detours")
                    assert window.command_entry_key(window.command_entry,
                        argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "gallery"
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[1]
                    assert window.mode == "files"
                    assert window.panes[1].content_stack.get_visible_child_name() == "files"
                    assert window.panes[0].content_stack.get_visible_child_name() == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "gallery"
                    assert all(pane.content_stack.get_visible_child_name() == "gallery" for pane in window.panes)
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[0] and window.mode == "gallery"
                    assert all(pane.content_stack.get_visible_child_name() == "gallery" for pane in window.panes)
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[1] and window.mode == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.get_focus() is window.active.view
                    assert window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_Tab, state=0))
                    assert window.active is window.panes[0]
                    assert window.mode == "gallery"
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.mode == "gallery" and window.get_visible()
                    window.key(window, argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.get_focus() is window.active.view
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
                    assert window.capture_browser_key(None, Gdk.KEY_Insert, 0, Gdk.ModifierType(0))
                    assert path in pane.marked_paths
                    assert window.get_focus() is pane.view
                    assert window.command_entry.get_text() == command_text
                    pane.marked_paths.discard(path)
                    window.refresh_file_footer(pane)
                    checks.append("Insert marks files after Gallery without moving focus to the command line")
                    checks.append("Ctrl+P and Ctrl+I switch modes; Escape preserves persistent views")
                    window.set_mode("gallery")
                    step["number"] = 151
                elif number == 151:
                    pane = window.panes[0]
                    if pane.gallery.building or not widget_children(pane.gallery.flow):
                        return True
                    window.focus_active_browser()
                    assert window.mode_shortcut("p") and window.mode == "project"
                    assert window.mode_shortcut("p") and window.mode == "gallery"
                    checks.append("Ctrl+P returns to Gallery when Project was opened there")
                    focused_card = pane.gallery.flow.get_focus_child()
                    assert focused_card is not None
                    assert window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_i, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "files"
                    assert window.get_focus() is pane.view
                    cursor = pane.view.get_cursor()[1]
                    assert cursor is not None
                    assert pane.view.path_is_selected(cursor)
                    assert pane.store[cursor][3] == str(focused_card.file_path)
                    step["number"] = 152
                elif number == 152:
                    assert window.mode == "files"
                    assert window.get_focus() is window.panes[0].view
                    checks.append("Gallery Ctrl+I keeps Commander focused after the key event")
                    window.set_mode("gallery")
                    window.panes[0].navigate(child / "assets")
                    step["number"] = 16
                elif number == 16:
                    pane = window.panes[0]
                    if (pane.path != child / "assets" or pane.gallery.displayed_path != pane.path
                            or pane.gallery.building or not widget_children(window.gallery_flow)
                            or pane.gallery.layout() != (pane.gallery.columns, pane.gallery.capacity)):
                        return True
                    assert window.mode == "gallery"
                    parent_card = widget_children(window.gallery_flow)[0]
                    assert parent_card.is_parent
                    assert window.gallery_flow.get_selected_children() == [parent_card]
                    assert window.gallery_flow.get_focus_child() is parent_card
                    file_cursor = pane.view.get_cursor()[1]
                    assert file_cursor is not None and pane.view.path_is_selected(file_cursor)
                    if not step.get("parent_gallery_files_checked"):
                        window.set_mode("files")
                        file_cursor = pane.view.get_cursor()[1]
                        assert window.get_focus() is pane.view
                        assert pane.view.path_is_selected(file_cursor)
                        assert pane.store[file_cursor][1] == ".."
                        window.set_mode("gallery")
                        step["parent_gallery_files_checked"] = True
                        return True
                    checks.append("gallery folder opens with a visible cursor on parent")
                    checks.append("Commander keeps the parent cursor after Gallery")
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_x, state=Gdk.ModifierType(0)))
                    assert window.command_entry.get_text() == "x"
                    assert window.get_focus().get_ancestor(Gtk.Entry) is window.command_entry
                    assert window.command_entry_key(window.command_entry,
                        argparse.Namespace(keyval=Gdk.KEY_Escape, state=0))
                    assert window.get_focus() is parent_card
                    window.command_entry.set_text("xyz")
                    assert window.active.gallery.key(window.gallery_flow,
                        argparse.Namespace(keyval=Gdk.KEY_BackSpace, state=0))
                    assert window.command_entry.get_text() == "xy"
                    assert window.get_focus() is parent_card
                    checks.append("gallery typing and Backspace use the shared command line")
                    assert window.active.gallery.click(window.active.gallery.scroll,
                        argparse.Namespace(button=1, type=Gdk.EventType.BUTTON_PRESS,
                                           state=Gdk.ModifierType(0), x=500, y=700))
                    assert window.gallery_flow.get_selected_children()[0].is_parent
                    checks.append("blank gallery space focuses the nearest card")
                    window.command_entry.set_text("cd ..")
                    window.execute_command_line()
                    assert window.mode == "gallery"
                    step["number"] = 17
                elif number == 17:
                    pane = window.panes[0]
                    if not step.get("gallery_parent_scan_started"):
                        if (pane.path != child or pane.gallery.displayed_path != child
                                or pane.gallery.building or not any(
                                item is not None and item.file_path.name == "visual.png"
                                for item in pane.gallery.children_by_logical)):
                            return True
                        assert window.mode == "gallery"
                        selected_gallery = window.gallery_flow.get_selected_children()
                        assert len(selected_gallery) == 1
                        assert selected_gallery[0].file_path == child / "assets"
                        assert window.gallery_flow.get_focus_child() is selected_gallery[0]
                        assert window.active.gallery.key(window.gallery_flow,
                            argparse.Namespace(keyval=Gdk.KEY_Home, state=0))
                        assert window.gallery_flow.get_focus_child().gallery_index == 0
                        assert window.active.gallery.key(window.gallery_flow,
                            argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                        assert window.gallery_flow.get_focus_child().gallery_index == 1
                        _columns, pitch, _first_y = geometry(
                            pane.gallery.flow, widget_children(pane.gallery.flow))
                        if (pane.gallery.scroll.get_vadjustment().get_upper()
                                - pane.gallery.scroll.get_vadjustment().get_page_size()) < 3 * pitch:
                            return True
                        pane.gallery.scroll.get_vadjustment().set_value(3 * pitch)
                        pane.gallery.snap_scroll()
                        assert pane.gallery.top() > 0
                        checks.append("gallery folder navigation refreshes without losing selection state")
                        checks.append("typed cd navigates from gallery and returns its cursor")
                        pane.navigate(project)
                        step["gallery_parent_scan_started"] = True
                        return True
                    if (pane.path != project or pane.gallery.displayed_path != project
                            or pane.gallery.building or not widget_children(pane.gallery.flow)):
                        return True
                    selected_gallery = pane.gallery.flow.get_selected_children()
                    assert len(selected_gallery) == 1
                    assert selected_gallery[0].file_path == child
                    top = pane.gallery.top()
                    if not top <= selected_gallery[0].gallery_row < top + pane.gallery.capacity:
                        return True
                    assert pane.gallery.flow.get_focus_child() is selected_gallery[0]
                    checks.append("gallery folder changes show the selected card without old scroll offset")
                    window.set_mode("files")
                    assert window.get_focus() is pane.view
                    original_rebuild = pane.rebuild_entries
                    step["original_rebuild"] = original_rebuild

                    def observe_large_rebuild(*rebuild_args, **rebuild_kwargs):
                        result = original_rebuild(*rebuild_args, **rebuild_kwargs)
                        if (pane.path == bulk and len(pane.entries) > 256
                                and not step.get("bulk_first_seen")):
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
                    assert pane.cursor_logical() == len(pane.entries) - 1, (
                        pane.cursor_logical(), len(pane.entries), pane.model_pending_target,
                        pane.visible_top(), pane.capacity, pane.view.get_cursor())
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
                    from workspace import pane as pane_module
                    original_listing = pane_module.directory_entries

                    def delayed_listing(folder, hidden=False, on_batch=None, cancelled=None,
                                        include_created=False):
                        if Path(folder) != slow:
                            return original_listing(folder, hidden, on_batch, cancelled, include_created)

                        def delayed_batch(batch):
                            on_batch(batch)
                            slow_continue.wait()

                        return original_listing(folder, hidden, delayed_batch, cancelled, include_created)

                    pane_module.directory_entries = delayed_listing
                    window.panes[0].navigate(slow)
                    step["number"] = 21
                elif number == 21:
                    pane = window.panes[0]
                    if pane.path != slow or not pane.listed_rows:
                        return True
                    assert "loading" in pane.count.get_text().lower()
                    assert pane.cursor_logical() == 0
                    assert len(pane.entries) < 401
                    checks.append("slow scans expose partial results with a cursor")
                    screenshot("slow-partial")
                    slow_continue.set()
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
                    if not window.shells.terminals[0][0].workspace_ready:
                        return True
                    assert pane.cursor_logical() is not None
                    checks.append("typed cd validates unknown folders off the GTK thread")
                    window.shells.select(0)
                    assert window.shells.current.workspace_browser["paths"][0] == child
                    assert window.project_context_path == child
                    window.shells.select(1)
                    assert window.panes[0].path == bulk
                    checks.append("workspace tabs retain independent browser folders")
                    tab = window.shells.terminals[1][0]
                    folder = child / "assets"
                    shell_folder = tab.workspace_cwd
                    pending = len(tab.workspace_pending)
                    window.panes[0].navigate(folder)
                    window.set_mode("console")
                    assert tab.workspace_cwd == shell_folder
                    assert tab.workspace_tab_button.get_label() == str(shell_folder)
                    assert len(tab.workspace_pending) == pending
                    window.set_mode("files")
                    window.set_mode("gallery")
                    window.panes[0].navigate(child)
                    window.set_mode("console")
                    assert len(tab.workspace_pending) == pending
                    assert tab.workspace_cwd == shell_folder
                    checks.append("browser navigation never sends cd from Files or Gallery")
                    window.shells.select(0)
                    window.set_mode("console")
                    shell = window.shells.terminals[0][0]
                    shell.feed_child(b"sleep 2\n")
                    step["number"] = 24
                elif number == 24:
                    shell = window.shells.terminals[0][0]
                    if not window.shells.has_foreground_program(shell):
                        return True
                    assert not window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert not window.key(window,
                        argparse.Namespace(keyval=Gdk.KEY_p, state=Gdk.ModifierType.CONTROL_MASK))
                    assert not window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_p, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "console"
                    checks.append("Ctrl+O and Ctrl+P reach foreground terminal programs")
                    step["number"] = 25
                elif number == 25:
                    shell = window.shells.terminals[0][0]
                    if window.shells.has_foreground_program(shell):
                        return True
                    assert window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.mode == "project"
                    checks.append("Ctrl+O switches Nightty views at the shell prompt")
                    assert window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_o,
                                           state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
                    assert window.mode == "project"
                    checks.append("Ctrl+Shift+O sends a literal key to the terminal")
                    assert window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_Return, state=Gdk.ModifierType.CONTROL_MASK))
                    assert window.shells.terminal_shortcut(shell,
                        argparse.Namespace(keyval=Gdk.KEY_Return, state=Gdk.ModifierType.SHIFT_MASK))
                    assert shell.get_allow_hyperlink()
                    assert shell.workspace_url_tag >= 0 and shell.workspace_path_tag >= 0
                    checks.append("Ctrl+Enter and Shift+Enter reach the shell as newlines; terminal links register")
                    step["short_tab"] = window.shells.new(command=["/bin/sh", "-c", "cd /; exec /bin/sh"])
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
                    if short_tab in [terminal for terminal, _pid in window.shells.terminals]:
                        return True
                    assert len(window.shells.terminals) == 2
                    assert window.shells.current is window.shells.terminals[1][0]
                    checks.append("exiting shell tabs close without changing other tabs")
                    window.activate_pane(window.panes[0])
                    window.panes[0].navigate(child)
                    window.set_mode("gallery")
                    window.set_size_request(1400, 560)
                    window.set_default_size(1400, 560)
                    step["number"] = 28
                elif number == 28:
                    pane = window.panes[0]
                    window.queue_resize()
                    if (pane.path != child or len(pane.gallery.children_by_logical) < 70
                            or any(item is None for item in pane.gallery.children_by_logical)
                            or pane.gallery.scroll.get_allocated_height() > 600
                            or pane.gallery.resize_source):
                        return True
                    if pane.gallery.layout() != (pane.gallery.columns, pane.gallery.capacity):
                        pane.gallery.apply_resize()
                        return True
                    assert pane.gallery.columns == 2
                    step["small_gallery_capacity"] = pane.gallery.capacity
                    step["small_gallery_height"] = pane.gallery.scroll.get_allocated_height()
                    current = pane.gallery.children_by_logical[pane.gallery.capacity - 1]
                    pane.gallery.flow.unselect_all()
                    pane.gallery.flow.select_child(current)
                    current.grab_focus()
                    window.set_size_request(1400, 940)
                    window.set_default_size(1400, 940)
                    step["number"] = 29
                elif number == 29:
                    pane = window.panes[0]
                    window.queue_resize()
                    if (pane.gallery.scroll.get_allocated_height() <= step["small_gallery_height"] + 100
                            or pane.gallery.capacity <= step["small_gallery_capacity"]
                            or pane.gallery.resize_source
                            or any(item is None for item in pane.gallery.children_by_logical)):
                        return True
                    if pane.gallery.layout() != (pane.gallery.columns, pane.gallery.capacity):
                        pane.gallery.apply_resize()
                        return True
                    old_bottom = pane.gallery.children_by_logical[step["small_gallery_capacity"] - 1]
                    assert pane.gallery.flow.get_selected_children() == [old_bottom]
                    assert window.get_focus() is pane.gallery.flow.get_focus_child()
                    assert pane.gallery.flow.get_focus_child() in pane.gallery.flow.get_selected_children()
                    assert window.active.gallery.key(pane.gallery.flow,
                        argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
                    next_card = pane.gallery.child_for_index(step["small_gallery_capacity"], pane.gallery.top())
                    assert next_card in pane.gallery.flow.get_selected_children()
                    assert next_card.get_allocation().x == old_bottom.get_allocation().x
                    screenshot("gallery-resized")
                    checks.append("Gallery Down follows the resized column to its new bottom")
                    window.set_mode("files")
                    step["old_split_width"] = window.file_panes.get_allocated_width()
                    window.set_size_request(1600, 940)
                    window.set_default_size(1600, 940)
                    step["number"] = 30
                elif number == 30:
                    window.queue_resize()
                    width = window.file_panes.get_allocated_width()
                    if (width < step["old_split_width"] + 100
                            or window.pane_split_width != width):
                        return True
                    assert abs(window.file_panes.get_position() / width - .5) < .02
                    window.file_panes.set_position(round(width * .65))
                    assert abs(window.pane_split_ratio - .65) < .02, (
                        window.pane_split_ratio, width, window.file_panes.get_position(),
                        window.panes[0].get_width(), window.panes[1].get_width())
                    window.set_size_request(1200, 940)
                    window.set_default_size(1200, 940)
                    step["number"] = 31
                elif number == 31:
                    window.queue_resize()
                    width = window.file_panes.get_allocated_width()
                    if width > 1300:
                        return True
                    assert abs(window.file_panes.get_position() / width - .65) < .02
                    checks.append("panes start evenly and retain an intentional split across resize")
                    step["old_file_capacity"] = window.panes[0].capacity
                    window.set_size_request(1200, 560)
                    window.set_default_size(1200, 560)
                    step["number"] = 32
                elif number == 32:
                    window.queue_resize()
                    pane = window.panes[0]
                    if (pane.capacity >= step["old_file_capacity"]
                            or pane.capacity != pane.visible_capacity()
                            or pane.model_fill_source
                            or (pane.scroll.get_vadjustment().get_upper()
                                - pane.scroll.get_vadjustment().get_page_size()
                                < pane.max_top() * pane.item_pitch() - pane.item_pitch())):
                        return True
                    adjustment = pane.scroll.get_vadjustment()
                    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
                    pane.snap_scroll()
                    top = pane.visible_top()
                    assert top == pane.max_top(), (
                        top, pane.max_top(), pane.capacity, pane.item_pitch(),
                        adjustment.get_upper(), adjustment.get_page_size(), adjustment.get_value())
                    assert pane.store[2 * (top + pane.capacity - 1) + 1][3] == pane.entries[-1][3]
                    checks.append("file columns stay full after a height resize")
                    adjustment = pane.scroll.get_vadjustment()
                    pitch = pane.item_pitch()
                    aligned_offset = (pane.top_inset - pane.scroll.get_allocated_height()) % pitch
                    adjustment.set_value(3 * pitch + aligned_offset)
                    screenshot("row-boundary")
                    step["number"] = 33
                elif number == 33:
                    assert len(window.shells.terminals) == 2
                    step["row_geometry"] = {"top_inset": window.panes[0].top_inset,
                                            "pitch": window.panes[0].row_pitch,
                                            "capacity": window.panes[0].capacity}
                    pane = window.panes[0]
                    selected = pane.current_path()
                    pane.set_sort("size")
                    assert pane.sort_button.get_label() == "s"
                    assert pane.current_path() == selected
                    pane.set_sort("size")
                    assert pane.sort_button.get_label() == "S"
                    pane.set_sort("name")
                    assert pane.sort_button.get_label() == "n"
                    window.show_workspace_menu()
                    workspace_menu = window.workspace_button.nightty_popover
                    assert any("Project" in item.get_label()
                               for item in workspace_menu.nightty_items)
                    workspace_menu.popdown()
                    assert window.key(window, argparse.Namespace(
                        keyval=Gdk.KEY_F1, state=Gdk.ModifierType.CONTROL_MASK))
                    menu = pane.locations_button.nightty_popover
                    assert menu.nightty_category == "location"
                    step["number"] = 330
                elif number == 330:
                    pane = window.panes[0]
                    menu = pane.locations_button.nightty_popover
                    assert window.pane_menu_key(pane, menu, pane.locations_button, menu,
                        argparse.Namespace(keyval=Gdk.KEY_Left, state=0))
                    step["number"] = 331
                elif number == 331:
                    pane = window.panes[0]
                    menu = pane.view_button.nightty_popover
                    assert menu.nightty_category == "view"
                    assert menu.get_parent() is pane.view_button and menu.get_has_arrow()
                    assert any("Gallery" in item.get_label() for item in menu.nightty_items)
                    menu.popdown()
                    checks.append("Ctrl+F1 then Left reaches View; sort reverses and preserves selection")
                    window.shells.terminals[0][0].feed_child(b"exit\n")
                    step["number"] = 34
                elif number == 34:
                    if len(window.shells.terminals) != 1:
                        return True
                    checks.append("exiting a nonfinal shell keeps Nightty open")
                    window.shells.terminals[0][0].feed_child(b"exit\n")
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
        slow_continue.set()
        if app.window is not None:
            for job in app.window.operations.jobs:
                job.cancel.set()
        if app.result == 0:
            if capture_only:
                assert step["number"] == 53
                print(json.dumps({"screenshots": ["/tmp/workspace-commander.png"]}), flush=True)
            elif controls_only:
                assert step["number"] == 52
                print(json.dumps({"checks": checks,
                                  "screenshots": ["/tmp/workspace-terminal-start.png",
                                                  "/tmp/workspace-files-controls.png",
                                                  "/tmp/workspace-rapid-up-root.png"]}), flush=True)
            else:
                assert step["number"] == 35 and not app.window.shells.terminals
                checks.append("exiting the final shell quits Nightty")
                print(json.dumps({"checks": checks,
                    "bin_loaded_seconds": step["bin_loaded_seconds"],
                    "row_geometry": step["row_geometry"],
                    "screenshots": ["/tmp/workspace-" + mode + ".png" for mode in ("terminal-start", "footer-japanese", "files", "files-marked", "files-next-page", "files-end", "files-scrolled", "row-boundary", "search", "quick-view", "gallery-top", "gallery", "gallery-resized", "inspect-image", "inspect-pdf", "project", "console", "agents", "command-output", "bin", "slow-partial")]}), flush=True)
        return app.result
