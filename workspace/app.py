# SPDX-License-Identifier: MPL-2.0
"""GTK filesystem Surface. All slow IO is outside GTK's main thread."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
from urllib.parse import urlsplit
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, Vte

from .agents import Agents
from .appearance import load_appearance, stylesheet
from .model import State, Transfer, context_for, directory_entries


class Box(Gtk.Box):
    """Keep Nightty's start/end packing order while using GTK 4 boxes."""

    def __init__(self, **kwargs):
        margin = kwargs.pop("margin", 0)
        super().__init__(**kwargs)
        if margin:
            for edge in ("start", "end", "top", "bottom"):
                getattr(self, f"set_margin_{edge}")(margin)
        self._start_tail = None

    def _layout_child(self, child, expand, padding):
        if self.get_orientation() == Gtk.Orientation.HORIZONTAL:
            child.set_hexpand(expand)
            child.set_margin_start(padding)
            child.set_margin_end(padding)
        else:
            child.set_vexpand(expand)
            child.set_margin_top(padding)
            child.set_margin_bottom(padding)

    def pack_start(self, child, expand=False, _fill=False, padding=0):
        self._layout_child(child, expand, padding)
        if self._start_tail is not None and self._start_tail.get_parent() is not self:
            self._start_tail = None
        if self._start_tail is None:
            self.prepend(child)
        else:
            self.insert_child_after(child, self._start_tail)
        self._start_tail = child

    def pack_end(self, child, expand=False, _fill=False, padding=0):
        self._layout_child(child, expand, padding)
        if self._start_tail is not None and self._start_tail.get_parent() is not self:
            self._start_tail = None
        if self._start_tail is None:
            self.prepend(child)
        else:
            self.insert_child_after(child, self._start_tail)

    def get_children(self):
        return widget_children(self)


def widget_children(widget):
    result = []
    child = widget.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


def connect_key(widget, callback, capture=False):
    controller = Gtk.EventControllerKey.new()
    controller.set_propagation_phase(
        Gtk.PropagationPhase.CAPTURE if capture else Gtk.PropagationPhase.BUBBLE)
    controller.connect("key-pressed", lambda _controller, keyval, _keycode, state:
                       bool(callback(widget, argparse.Namespace(keyval=keyval, state=state))))
    widget.add_controller(controller)
    return controller


def connect_focus(widget, callback):
    controller = Gtk.EventControllerFocus.new()
    controller.connect("enter", lambda *_: callback(widget, None))
    widget.add_controller(controller)
    return controller


def connect_click(widget, callback):
    gesture = Gtk.GestureClick.new()
    gesture.set_button(0)

    def pressed(controller, _count, x, y):
        event = argparse.Namespace(button=controller.get_current_button(),
                                   type=Gdk.EventType.BUTTON_PRESS,
                                   state=controller.get_current_event_state(),
                                   x=x, y=y,
                                   gdk_event=controller.get_current_event())
        callback(widget, event)

    gesture.connect("pressed", pressed)
    widget.add_controller(gesture)
    return gesture


def on_resize(widget, callback):
    """GTK 4 has no size-allocate signal; watch mapped frame allocations."""
    previous = None

    def tick(current, _clock):
        nonlocal previous
        size = (current.get_allocated_width(), current.get_allocated_height())
        if size != previous:
            previous = size
            callback(current, argparse.Namespace(width=size[0], height=size[1]))
        return True

    widget.add_tick_callback(tick)


def text_has_focus(focus):
    return (focus is not None and
            (isinstance(focus, (Gtk.Entry, Gtk.TextView, Vte.Terminal))
             or focus.get_ancestor(Gtk.Entry) is not None
             or focus.get_ancestor(Gtk.TextView) is not None))


def drawing_overlay(widget, draw):
    overlay = Gtk.Overlay()
    overlay.set_child(widget)
    area = Gtk.DrawingArea()
    area.set_hexpand(True)
    area.set_vexpand(True)
    area.set_can_target(False)
    area.set_draw_func(lambda _area, context, _width, _height: draw(area, context))
    overlay.add_overlay(area)
    overlay.drawing_area = area
    return overlay


def label(text="", css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_selectable(True)
    widget.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda *_: callback())
    return widget


def yes_no_dialog(parent, text, secondary=None):
    dialog = Gtk.Dialog(transient_for=parent, modal=True, use_header_bar=False)
    dialog.set_decorated(False)
    dialog.set_resizable(False)
    dialog.add_css_class("mc-dialog")
    content = dialog.get_content_area()
    content.set_spacing(0)

    title = Gtk.Label(label=" Confirm ")
    title.add_css_class("mc-dialog-title")
    content.append(title)

    message = Gtk.Label(label=text, xalign=0)
    message.set_wrap(True)
    message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    message.set_max_width_chars(54)
    message.add_css_class("mc-dialog-message")
    content.append(message)
    if secondary:
        detail = Gtk.Label(label=secondary, xalign=0)
        detail.set_wrap(True)
        detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail.set_max_width_chars(54)
        detail.add_css_class("mc-dialog-detail")
        content.append(detail)

    yes = dialog.add_button("_Yes", Gtk.ResponseType.YES)
    no = dialog.add_button("_No", Gtk.ResponseType.NO)
    for choice in (yes, no):
        choice.add_css_class("mc-dialog-button")
    yes.add_css_class("mc-dialog-default")
    dialog.set_default_response(Gtk.ResponseType.YES)
    return dialog


def confirm(parent, text, secondary, on_yes, on_no=None):
    dialog = yes_no_dialog(parent, text, secondary)

    def answered(_dialog, response):
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            on_yes()
        elif on_no is not None:
            on_no()

    dialog.connect("response", answered)
    dialog.present()
    return dialog


def scrolled(widget):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_child(widget)
    return scroll


def textview():
    view = Gtk.TextView(editable=False, cursor_visible=False,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(24)
    view.set_right_margin(24)
    view.set_top_margin(20)
    view.set_bottom_margin(20)
    return view


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


class Pane(Box):
    def __init__(self, window, folder, index):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window, self.path, self.index = window, Path(folder), index
        self.previous_path = None
        self.hidden, self.generation = False, 0
        self.gallery_serial = 0
        self.gallery_capacity, self.gallery_columns = 1, 2
        self.gallery_resize_source = 0
        self.gallery_building = False
        self.gallery_scroll_snap_source = 0
        self.gallery_children_by_logical = []
        self.gallery_replicas_by_logical = {}
        self.gallery_selection_syncing = False
        self.gallery_displayed_path = None
        self.gallery_build_waiting = False
        self.gallery_preview_results = {}
        self.gallery_render_waiters = {}
        self.gallery_pending_target = None
        self.gallery_pending_top = None
        toolbar = Box(spacing=5, margin=7)
        self.locations_button = button("⌄", lambda: self.window.show_locations(self))
        self.locations_button.set_tooltip_text("Favourites and recent folders (Alt+1 / Alt+2)")
        toolbar.pack_start(self.locations_button, False, False, 0)
        self.location = Gtk.Entry()
        self.location.set_text(str(self.path))
        self.location.connect("activate", lambda entry: self.navigate(Path(entry.get_text()).expanduser()))
        toolbar.pack_start(self.location, True, True, 0)
        search_button = Gtk.Button()
        search_button.set_child(Gtk.Image.new_from_icon_name("edit-find-symbolic"))
        search_button.set_tooltip_text("Search names in this folder (Alt+S)")
        search_button.connect("clicked", lambda *_: self.show_search())
        toolbar.pack_start(search_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)
        self.search_row = Box(spacing=6, margin=7)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search filenames in this folder")
        self.search.connect("changed", self.search_changed)
        self.search.connect("activate", lambda *_: self.hide_search())
        connect_key(self.search, self.search_key)
        self.search_row.pack_start(self.search, True, True, 0)
        self.search_result = label("", "muted")
        self.search_row.pack_start(self.search_result, False, False, 0)
        self.search_row.pack_start(button("×", self.hide_search), False, False, 0)
        self.pack_start(self.search_row, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.view = Gtk.IconView(model=self.store)
        self.view.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.view.set_columns(2)
        self.view.set_item_width(300)
        self.view.set_item_padding(1)
        self.view.set_row_spacing(0)
        self.view.set_column_spacing(2)
        self.view.set_margin(0)
        self.view.set_item_orientation(Gtk.Orientation.HORIZONTAL)
        self.entries = []
        self.capacity = 1
        self.logical_to_stores = {}
        self.store_to_logical = {}
        self.path_to_stores = {}
        self.row_pitch = None
        self.top_inset = 4
        self.geometry_measured = False
        self.geometry_attempts = 0
        self.scroll_snap_source = 0
        self.snapping_scroll = False
        self.align_after_navigation = False
        self.navigation_align_serial = 0
        self.layout_pending = False
        self.selection_anchor = 0
        self.model_fill_source = 0
        self.model_next_start = 0
        self.model_pending_target = None
        self.model_pending_select = False
        self.model_pending_range = None
        self.model_selected_paths = set()
        self.model_layout_target = None
        self.marked_paths = set()
        self.entry_sizes = {}
        self.info_cache = {}
        self.footer_serial = 0
        self.space_serial = 0
        self.staging_store = None
        self.staging_next_start = 0
        self.staging_logical_to_stores = {}
        self.staging_store_to_logical = {}
        self.staging_path_to_stores = {}
        self.directory_paths = set()
        self.listed_rows = []
        self.scan_rows = []
        self.scan_flush_source = 0
        self.placeholder_source = 0
        self.pending_restore_name = None
        # IconView otherwise gives fallback fonts (for example Japanese names)
        # taller cells, breaking the fixed-height two-column navigation grid.
        self.row_text_height = self.view.create_pango_layout("Ag").get_pixel_size()[1]
        marker = Gtk.CellRendererText(xpad=1, ypad=0, width_chars=1,
                                      weight=self.window.appearance["fonts"]["files_weight"])
        marker.set_fixed_size(-1, self.row_text_height)
        self.view.pack_start(marker, False)
        self.view.set_cell_data_func(marker, self.prefix_color)
        renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, xpad=2, ypad=0,
                                        weight=self.window.appearance["fonts"]["files_weight"])
        renderer.set_fixed_size(-1, self.row_text_height)
        self.renderer = renderer
        self.view.pack_start(renderer, True)
        self.view.add_attribute(renderer, "text", 1)
        self.view.set_cell_data_func(renderer, self.name_color)
        connect_key(self.view, self.move_by_key)
        connect_click(self.view, self.focus_nearest_on_blank)
        connect_focus(self.view, lambda *_: self.window.activate_pane(self))
        self.view.connect("item-activated", self.open)
        self.cursor_handler = self.view.connect("selection-changed", self.selected)
        self.scroll = scrolled(self.view)
        on_resize(self.scroll, self.resize_columns)
        self.scroll.get_vadjustment().connect("value-changed", self.scroll_changed)
        self.scroll_overlay = drawing_overlay(self.scroll, self.draw_column_divider)
        self.content_stack = Gtk.Stack()
        self.content_stack.set_vhomogeneous(False)
        self.content_stack.add_named(self.scroll_overlay, "files")
        self.list_start = Box()
        self.list_start.add_css_class("pane-list-rule")
        self.list_start.set_size_request(-1, 2)
        self.pack_start(self.list_start, False, False, 0)
        self.pack_start(self.content_stack, True, True, 0)
        self.list_end = Box()
        self.list_end.add_css_class("pane-list-rule")
        self.list_end.set_size_request(-1, 2)
        self.pack_start(self.list_end, False, False, 0)
        self.status_box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.status_box.add_css_class("footer-box")
        self.status = label("", "footer-details")
        self.status.set_selectable(False)
        self.status_box.pack_start(self.status, False, False, 0)
        folder_row = Box(spacing=12)
        self.folder_path = label("", "footer-folder")
        self.folder_path.set_selectable(False)
        folder_row.pack_start(self.folder_path, True, True, 0)
        self.folder_stats = label("", "footer-folder-stats")
        self.folder_stats.set_selectable(False)
        folder_row.pack_end(self.folder_stats, False, False, 0)
        self.status_box.pack_start(folder_row, False, False, 0)
        self.pack_start(self.status_box, False, False, 0)
        self.count = label("Loading…", "muted")
        self.count.set_selectable(False)
        self.search_row.hide()

    def navigate(self, path, selected_name=None):
        if self.search_row.get_visible():
            self.hide_search()
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = self.path / path
        path = path.absolute()
        self.info_cache.clear()
        if path != self.path:
            self.marked_paths.clear()
        restore_name = selected_name if selected_name is not None else self.path.name if path == self.path.parent else None
        self.generation += 1
        generation = self.generation
        self.stop_model_fill()
        if self.scan_flush_source:
            GLib.source_remove(self.scan_flush_source)
            self.scan_flush_source = 0
        if self.placeholder_source:
            GLib.source_remove(self.placeholder_source)
            self.placeholder_source = 0
        self.scan_rows = []
        self.listed_rows = []
        self.pending_restore_name = restore_name
        if path != self.path:
            self.previous_path = self.path
        self.path = path
        self.location.set_text(str(path))
        self.count.set_text("Loading…")
        announced = hasattr(self.window, "active") and hasattr(self.window, "status")
        if announced:
            self.window.state.remember_folder(path)
            self.window.directory_changed(self)

            def show_placeholder():
                self.placeholder_source = 0
                if generation == self.generation and not self.scan_rows:
                    self.show_directory_rows([], loading=True)
                return False

            self.placeholder_source = GLib.timeout_add(40, show_placeholder)

        batches = queue.SimpleQueue()

        def flush():
            if generation != self.generation:
                self.scan_flush_source = 0
                return False
            changed = False
            while True:
                try:
                    self.scan_rows.extend(batches.get_nowait())
                    changed = True
                except queue.Empty:
                    break
            if changed and hasattr(self.window, "active"):
                rows = sorted(self.scan_rows, key=lambda row: (not row[1], row[0].casefold()))
                self.show_directory_rows(rows, loading=True)
            return True

        self.scan_flush_source = GLib.timeout_add(80, flush)

        def loaded(rows):
            if generation != self.generation:
                return
            if self.placeholder_source:
                GLib.source_remove(self.placeholder_source)
                self.placeholder_source = 0
            if self.scan_flush_source:
                GLib.source_remove(self.scan_flush_source)
                self.scan_flush_source = 0
            if not announced:
                self.window.state.remember_folder(path)
                self.window.directory_changed(self)
            self.show_directory_rows(rows)
            if self.content_stack.get_visible_child_name() == "gallery":
                self.window.show_gallery(self)
            if self.window.quick_enabled:
                if self.index == 1:
                    self.window.quick_destination.set_text(str(path))
                elif not self.entries:
                    self.window.update_quick_view()

        def failed(exc):
            if generation != self.generation:
                return
            if self.placeholder_source:
                GLib.source_remove(self.placeholder_source)
                self.placeholder_source = 0
            if self.scan_flush_source:
                GLib.source_remove(self.scan_flush_source)
                self.scan_flush_source = 0
            self.show_directory_rows([])
            self.count.set_text(f"Cannot open {path}: {exc}")
            self.window.refresh_folder_footer(self)

        self.window.async_call(lambda: directory_entries(path, self.hidden,
            on_batch=batches.put, cancelled=lambda: generation != self.generation), loaded, failed,
            executor=self.window.listing_pool)

    def show_directory_rows(self, rows, loading=False):
        path = self.path
        keep_focus = (self is self.window.active and self.window.mode == "files"
                      and self.window.get_focus() is self.view)
        self.listed_rows = rows
        old_entries = self.entries
        old_cursor_path = None
        if self.view.get_cursor()[0]:
            cursor_path = self.view.get_cursor()[1]
            if cursor_path is not None and cursor_path.get_indices()[0] < len(self.store):
                old_cursor_path = self.store[cursor_path][3]
        entries = []
        directories = set()
        if path.parent != path:
            parent = str(path.parent)
            entries.append(["/..", "..", "", parent, self.window.palette["directory"]])
            directories.add(parent)
        for name, directory, symlink, size, executable in rows:
            prefix = "@" if symlink else "/" if directory else "*" if executable else ""
            entry_path = str(path / name)
            entries.append([prefix + name, name,
                "" if directory else self.window.size(size), entry_path,
                self.window.file_color(name, directory, symlink, executable)])
            if directory:
                directories.add(entry_path)
        self.entries = entries
        self.entry_sizes = {str(path / name): size for name, directory, _symlink, size, _exec in rows
                            if not directory}
        if not loading:
            self.marked_paths.intersection_update(row[3] for row in entries)
        self.directory_paths = directories
        if loading:
            self.count.set_text(f"{len(rows)} found · loading…")
        else:
            self.count.set_text(f"{len(rows)} entries" + (" · hidden included" if self.hidden else ""))
        self.window.refresh_folder_footer(self)
        restore_found = (self.pending_restore_name is not None and
                         any(row[1] == self.pending_restore_name for row in entries))
        self.rebuild_entries(restore_name=self.pending_restore_name,
                             preserve=bool(old_entries and not restore_found and
                                           old_cursor_path in {row[3] for row in entries}))
        if keep_focus:
            self.view.grab_focus()
        self.window.refresh_file_footer(self)
        if restore_found:
            self.pending_restore_name = None

    def up(self):
        self.navigate(self.path.parent)

    def refresh(self):
        current = self.cursor_logical()
        name = self.entries[current][1] if current is not None else None
        self.navigate(self.path, selected_name=name)

    def favourite(self):
        self.window.state.toggle_favourite(self.path)

    def show_search(self):
        self.window.set_mode("files")
        self.window.activate_pane(self, update=self.window.active is not self)
        self.search_row.show()
        self.search.grab_focus()
        self.search.select_region(0, -1)

    def hide_search(self):
        self.search_row.hide()
        self.search.set_text("")
        self.view.grab_focus()

    def search_changed(self, *_):
        query = self.search.get_text().casefold().strip()
        if not query:
            self.search_result.set_text("")
            self.model_pending_target = None
            self.model_pending_range = None
            return
        matches = [index for index, row in enumerate(self.entries)
                   if row[1] != ".." and query in row[1].casefold()]
        self.search_result.set_text(f"{len(matches)} found" if matches else "No matches")
        if matches:
            position = self.store_position(matches[0])
            if position is None:
                self.view.unselect_all()
                self.model_selected_paths.clear()
                self.model_pending_target = matches[0]
                self.model_pending_select = True
                self.model_pending_range = None
                return
            self.model_pending_target = None
            self.model_pending_range = None
            path = Gtk.TreePath.new_from_indices([position])
            self.view.unselect_all()
            self.view.set_cursor(path, None, False)
            self.view.select_path(path)
            self.view.scroll_to_path(path, False, 0, 0)
        else:
            self.model_pending_target = None
            self.model_pending_range = None

    def search_key(self, _entry, event):
        if Gdk.keyval_name(event.keyval) == "Escape":
            self.hide_search()
            return True
        return False

    def selected_paths(self):
        selected = sorted(self.view.get_selected_items(),
                          key=lambda path: self.store_to_logical.get(path.get_indices()[0], len(self.entries)))
        paths = []
        seen = set()
        for path in selected:
            entry_path = self.store[path][3]
            if entry_path and self.store[path][1] != ".." and entry_path not in seen:
                paths.append(Path(entry_path))
                seen.add(entry_path)
        return paths

    def current_path(self):
        logical = self.cursor_logical()
        if logical is None or logical >= len(self.entries) or self.entries[logical][1] == "..":
            return None
        return Path(self.entries[logical][3])

    def operation_paths(self):
        if self.marked_paths:
            return [Path(row[3]) for row in self.entries if row[3] in self.marked_paths]
        selected = self.selected_paths()
        return selected or ([self.current_path()] if self.current_path() is not None else [])

    def toggle_mark_current(self, advance=True):
        path = self.current_path()
        if path is None:
            return False
        key = str(path)
        if key in self.marked_paths:
            self.marked_paths.remove(key)
        else:
            self.marked_paths.add(key)
        self.view.queue_draw()
        self.window.refresh_file_footer(self)
        if advance:
            self.move_by_key(self.view, argparse.Namespace(keyval=Gdk.KEY_Down, state=0))
        return True

    def cursor_logical(self):
        has_cursor, path, _cell = self.view.get_cursor()
        return self.store_to_logical.get(path.get_indices()[0]) if has_cursor else None

    def focus_file_path(self, path=None):
        positions = self.path_to_stores.get(str(path), ()) if path is not None else ()
        if positions:
            logical = self.store_to_logical.get(positions[0])
            position = self.store_position(logical) if logical is not None else None
            cursor = Gtk.TreePath.new_from_indices([position]) if position is not None else None
        else:
            cursor = self.view.get_cursor()[1]
            if cursor is None and self.store:
                cursor = Gtk.TreePath.new_from_indices([0])
        if cursor is None:
            return
        self.view.set_cursor(cursor, None, False)
        self.view.select_path(cursor)
        self.view.scroll_to_path(cursor, False, 0.0, 0.0)

    def model_rows(self):
        """Enough rows for each screen to show adjacent full filename columns."""
        count = len(self.entries)
        return min(count, max(self.capacity, count - self.capacity))

    def max_top(self):
        return max(0, len(self.entries) - 2 * self.capacity)

    def visible_top(self):
        return min(self.max_top(), max(0, round(
            self.scroll.get_vadjustment().get_value() / max(1, self.item_pitch()))))

    def store_position(self, logical, top=None):
        positions = self.logical_to_stores.get(logical, ())
        if not positions:
            return None
        top = self.visible_top() if top is None else top
        return min(positions, key=lambda position:
                   max(top - position // 2, 0, position // 2 - top - self.capacity + 1))

    def select_logical(self, logical):
        for position in self.logical_to_stores.get(logical, ()):
            self.view.select_path(Gtk.TreePath.new_from_indices([position]))

    def nearest_logical(self, position):
        row, column = divmod(position, 2)
        logical = row + column * self.capacity
        if row < self.model_rows() and logical < len(self.entries):
            return logical
        return min(row, len(self.entries) - 1)

    def focus_nearest_on_blank(self, _view, event):
        if event.button == 3:
            path = self.view.get_path_at_pos(int(event.x), int(event.y))
            if path is None or not self.store[path][3]:
                return False
            self.window.activate_pane(self)
            self.view.grab_focus()
            self.view.unselect_all()
            self.view.set_cursor(path, None, False)
            self.view.select_path(path)
            self.selected()
            self.toggle_mark_current(advance=False)
            return True
        if event.button != 1:
            return False
        path = self.view.get_path_at_pos(int(event.x), int(event.y))
        if path is not None and self.store[path][3]:
            return False
        self.window.activate_pane(self, update=self.window.active is not self)
        self.view.grab_focus()
        if not self.entries:
            return True
        if path is None:
            probes = (self.view.get_item_width() // 2,
                      self.view.get_item_width() * 3 // 2 + self.view.get_column_spacing())
            nearby = [(abs(event.x - x), candidate) for x in probes
                      if (candidate := self.view.get_path_at_pos(x, int(event.y))) is not None
                      and self.store[candidate][3]]
            target = (self.store_to_logical[min(nearby, key=lambda item: item[0])[1].get_indices()[0]]
                      if nearby else len(self.entries) - 1)
        else:
            target = self.nearest_logical(path.get_indices()[0])
        cursor = Gtk.TreePath.new_from_indices([self.store_position(target)])
        self.view.handler_block(self.cursor_handler)
        self.view.unselect_all()
        self.view.set_cursor(cursor, None, False)
        self.view.select_path(cursor)
        self.view.handler_unblock(self.cursor_handler)
        self.view.scroll_to_path(cursor, False, 0.0, 0.0)
        self.selected()
        return True

    def move_by_key(self, _view, event):
        if event.state & Gdk.ModifierType.ALT_MASK:
            return False
        key = Gdk.keyval_name(event.keyval)
        if ((key in {"Insert", "KP_Insert"} or
             key.lower() == "t" and event.state & Gdk.ModifierType.CONTROL_MASK)
                and not event.state & Gdk.ModifierType.SUPER_MASK):
            return self.toggle_mark_current()
        if (key in {"Up", "KP_Up", "Down", "KP_Down"}
                and event.state & Gdk.ModifierType.SHIFT_MASK
                and not event.state & Gdk.ModifierType.CONTROL_MASK):
            self.toggle_mark_current(advance=False)
            return self.move_by_key(_view, argparse.Namespace(
                keyval=event.keyval, state=event.state & ~Gdk.ModifierType.SHIFT_MASK))
        if not self.entries:
            return self.window.command_key(event)
        current = self.cursor_logical()
        if current is None:
            current = 0
        capacity = self.capacity
        top = self.visible_top()
        if key in {"Down", "KP_Down"}:
            target = min(current + 1, len(self.entries) - 1)
        elif key in {"Up", "KP_Up"}:
            target = max(current - 1, 0)
        elif key in {"Right", "KP_Right"}:
            target = min(current + capacity, len(self.entries) - 1)
        elif key in {"Left", "KP_Left"}:
            target = max(current - capacity, 0)
        elif key in {"Home", "KP_Home"}:
            target = 0
        elif key in {"End", "KP_End"}:
            target = len(self.entries) - 1
        elif key in {"Page_Down", "KP_Page_Down"}:
            target = min(current + 2 * capacity, len(self.entries) - 1)
        elif key in {"Page_Up", "KP_Page_Up"}:
            target = max(current - 2 * capacity, 0)
        else:
            return self.window.command_key(event)
        if key in {"Home", "KP_Home"}:
            next_top = 0
        elif key in {"End", "KP_End"}:
            next_top = self.max_top()
        elif target < top:
            step = 2 * capacity if key in {"Page_Up", "KP_Page_Up"} else capacity
            next_top = max(0, min(target, top - step))
        elif target >= top + 2 * capacity:
            step = 2 * capacity if key in {"Page_Down", "KP_Page_Down"} else capacity
            next_top = min(self.max_top(), max(top + step, target - 2 * capacity + 1))
        else:
            next_top = top
        self.model_pending_target = None
        self.model_pending_range = None
        if target == current:
            return True
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        anchor = self.selection_anchor if shift else target
        if target not in self.logical_to_stores:
            earliest_row = target if target < capacity else target - capacity
            if earliest_row - self.model_next_start < capacity:
                self.view.handler_block(self.cursor_handler)
                self.append_model_page()
                self.view.handler_unblock(self.cursor_handler)
            if target not in self.logical_to_stores:
                if shift:
                    self.model_pending_range = (min(anchor, target), max(anchor, target))
                    self.model_pending_select = False
                    self.view.handler_block(self.cursor_handler)
                    for logical in self.logical_to_stores:
                        if self.model_pending_range[0] <= logical <= self.model_pending_range[1]:
                            self.select_logical(logical)
                    self.view.handler_unblock(self.cursor_handler)
                elif control:
                    self.model_pending_select = False
                else:
                    self.view.unselect_all()
                    self.model_selected_paths.clear()
                    self.model_pending_select = True
                self.model_pending_target = target
                self.selection_anchor = anchor
                return True
        self.scroll.get_vadjustment().set_value(next_top * self.item_pitch())
        path = Gtk.TreePath.new_from_indices([self.store_position(target, next_top)])
        self.view.handler_block(self.cursor_handler)
        if not control:
            self.view.unselect_all()
        self.view.set_cursor(path, None, False)
        if shift:
            for logical in range(min(anchor, target), max(anchor, target) + 1):
                self.select_logical(logical)
        elif not control:
            self.select_logical(target)
        self.view.handler_unblock(self.cursor_handler)
        self.view.scroll_to_path(path, False, 0.0, 0.0)
        self.selected()
        self.selection_anchor = anchor
        return True

    def visible_capacity(self):
        height = self.scroll.get_allocated_height()
        if not height:
            return 30
        pitch = self.item_pitch()
        return max(1, int((height - self.top_inset) // max(1, pitch)))

    def item_pitch(self):
        if self.row_pitch is None:
            self.row_pitch = (self.row_text_height + 2 * self.view.get_item_padding()
                              + self.view.get_row_spacing())
        return self.row_pitch

    def scroll_changed(self, _adjustment):
        if self.snapping_scroll or not self.geometry_measured or not self.entries:
            return
        if self.align_after_navigation and _adjustment.get_value() > 0:
            self.align_after_navigation = False
            if self.scroll_snap_source:
                GLib.source_remove(self.scroll_snap_source)
                self.scroll_snap_source = 0
            self.snap_scroll()
            return
        if self.scroll_snap_source:
            GLib.source_remove(self.scroll_snap_source)
        self.scroll_snap_source = GLib.timeout_add(45, self.snap_scroll)

    def clear_navigation_alignment(self, serial):
        if serial == self.navigation_align_serial:
            self.align_after_navigation = False
        return False

    def snap_scroll(self):
        self.scroll_snap_source = 0
        if self.model_layout_target is not None:
            return False
        adjustment = self.scroll.get_vadjustment()
        pitch = self.item_pitch()
        maximum = min(self.max_top() * pitch,
                      max(0, adjustment.get_upper() - adjustment.get_page_size()))
        last_row = int(maximum // pitch) * pitch
        target = min(round(adjustment.get_value() / pitch) * pitch, last_row)
        if abs(adjustment.get_value() - target) > 0.5:
            self.snapping_scroll = True
            adjustment.set_value(target)
            self.snapping_scroll = False
        self.keep_cursor_visible()
        return False

    def keep_cursor_visible(self):
        if not self.entries or self.model_layout_target is not None:
            return
        top = self.visible_top()
        current = self.cursor_logical()
        if current is not None and top <= current < top + 2 * self.capacity:
            return
        target = top if current is None or current < top else min(
            len(self.entries) - 1, top + 2 * self.capacity - 1)
        position = self.store_position(target, top)
        if position is None:
            return
        multiple_selected = len(self.selected_paths()) > 1
        self.view.handler_block(self.cursor_handler)
        if not multiple_selected:
            self.view.unselect_all()
            self.select_logical(target)
        self.view.set_cursor(Gtk.TreePath.new_from_indices([position]), None, False)
        self.view.handler_unblock(self.cursor_handler)
        self.selected()

    def rebuild_entries(self, restore_name=None, preserve=False):
        selected = {self.store[path][3] for path in self.view.get_selected_items()
                    if self.store[path][3]} if preserve else set()
        if preserve:
            selected.update(self.model_selected_paths)
        cursor_path = self.view.get_cursor()[1] if preserve else None
        cursor_target = self.store[cursor_path][3] if cursor_path is not None else None
        pending_target = self.model_pending_target if preserve else None
        pending_select = self.model_pending_select if preserve and pending_target is not None else False
        pending_range = self.model_pending_range if preserve and pending_target is not None else None
        if pending_select:
            selected.clear()
        self.stop_model_fill()
        previous_top = self.visible_top() if preserve else 0
        self.capacity = self.visible_capacity()
        self.view.handler_block(self.cursor_handler)
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.view.set_model(self.store)
        self.logical_to_stores.clear()
        self.store_to_logical.clear()
        self.path_to_stores.clear()
        self.model_selected_paths = selected
        self.model_next_start = 0
        self.model_pending_target = None
        self.model_pending_select = pending_select
        self.model_pending_range = pending_range
        if self.entries:
            self.append_model_page()
            if len(self.entries) <= 256:
                while self.model_next_start < self.model_rows():
                    self.append_model_page()
        if self.entries:
            restored = next((i for i, row in enumerate(self.entries)
                             if restore_name is not None and row[1] == restore_name), None)
            kept = next((i for i, row in enumerate(self.entries)
                         if cursor_target is not None and row[3] == cursor_target), None)
            index = (pending_target if pending_target is not None else
                     restored if restored is not None else kept if kept is not None else 0)
            self.model_pending_target = index if index not in self.logical_to_stores else None
            if self.model_pending_target is not None and pending_target is None:
                self.model_pending_select = not selected
            visible_index = index if self.model_pending_target is None else 0
            path = Gtk.TreePath.new_from_indices([self.store_position(visible_index)])
            self.view.set_cursor(path, None, False)
            if not selected:
                self.select_logical(visible_index)
            self.selection_anchor = visible_index
            if self.scroll_snap_source:
                GLib.source_remove(self.scroll_snap_source)
                self.scroll_snap_source = 0
            self.navigation_align_serial += 1
            self.align_after_navigation = True
            self.view.queue_resize()
            self.scroll.queue_resize()
            desired_top = min(previous_top, self.max_top()) if preserve else min(
                self.max_top(), max(0, visible_index - 2 * self.capacity + 1))
            self.scroll.get_vadjustment().set_value(desired_top * self.item_pitch())
            GLib.timeout_add(200, self.clear_navigation_alignment, self.navigation_align_serial)
        self.view.handler_unblock(self.cursor_handler)
        if self is self.window.active:
            self.selected()
        if self.model_next_start < self.model_rows():
            self.staging_store = Gtk.ListStore(str, str, str, str, str)
            self.staging_next_start = 0
            self.staging_logical_to_stores = {}
            self.staging_store_to_logical = {}
            self.staging_path_to_stores = {}
            self.model_fill_source = GLib.timeout_add(5, self.fill_model_page)
        if not self.geometry_measured and len(self.entries) > 2:
            self.geometry_attempts = 0
            GLib.timeout_add(30, self.measure_item_geometry)

    def stop_model_fill(self):
        if self.model_fill_source:
            GLib.source_remove(self.model_fill_source)
            self.model_fill_source = 0
        self.staging_store = None
        self.model_layout_target = None

    def append_model_page(self, staging=False):
        start = self.staging_next_start if staging else self.model_next_start
        total_rows = self.model_rows()
        if start >= total_rows:
            return
        store = self.staging_store if staging else self.store
        logical_to_stores = self.staging_logical_to_stores if staging else self.logical_to_stores
        store_to_logical = self.staging_store_to_logical if staging else self.store_to_logical
        path_to_stores = self.staging_path_to_stores if staging else self.path_to_stores
        empty = ["", "", "", "", ""]
        end = min(total_rows, start + self.capacity * (2 if staging else 1))
        for row in range(start, end):
            for logical in (row, row + self.capacity):
                position = len(store)
                if logical < len(self.entries):
                    entry = self.entries[logical]
                    store.append(entry)
                    logical_to_stores.setdefault(logical, []).append(position)
                    store_to_logical[position] = logical
                    path_to_stores.setdefault(entry[3], []).append(position)
                    if not staging and (entry[3] in self.model_selected_paths or
                            self.model_pending_range is not None and
                            self.model_pending_range[0] <= logical <= self.model_pending_range[1]):
                        self.view.select_path(Gtk.TreePath.new_from_indices([position]))
                else:
                    store.append(empty)
        if end == total_rows and len(self.entries) > 2 * self.capacity:
            store.append(empty)
            store.append(empty)
        if staging:
            self.staging_next_start = end
        else:
            self.model_next_start = end

    def fill_model_page(self):
        if self.staging_store is None:
            self.model_fill_source = 0
            return False
        self.append_model_page(staging=True)
        if self.staging_next_start < self.model_rows():
            return True
        self.model_fill_source = 0
        self.finish_model_fill()
        return False

    def finish_model_fill(self):
        keep_focus = (self is self.window.active and self.window.mode == "files"
                      and self.window.get_focus() is self.view)
        current = self.cursor_logical()
        target = self.model_pending_target if self.model_pending_target is not None else current
        selected = {self.store[path][3] for path in self.view.get_selected_items()
                    if self.store[path][3]} | self.model_selected_paths
        if self.model_pending_range is not None:
            first, last = self.model_pending_range
            selected.update(self.entries[logical][3] for logical in range(first, last + 1))
        if self.model_pending_select and target is not None:
            selected = {self.entries[target][3]}
        elif not selected and target is not None:
            selected = {self.entries[target][3]}
        self.view.handler_block(self.cursor_handler)
        self.view.set_model(self.staging_store)
        self.store = self.staging_store
        self.logical_to_stores = self.staging_logical_to_stores
        self.store_to_logical = self.staging_store_to_logical
        self.path_to_stores = self.staging_path_to_stores
        self.staging_store = None
        self.model_next_start = self.model_rows()
        if target is not None:
            path = Gtk.TreePath.new_from_indices([self.store_position(target)])
            self.view.set_cursor(path, None, False)
            self.selection_anchor = target
        for entry_path in selected:
            for position in self.path_to_stores.get(entry_path, ()):
                self.view.select_path(Gtk.TreePath.new_from_indices([position]))
        if target is not None:
            self.view.queue_resize()
            self.scroll.queue_resize()
            desired_top = min(self.max_top(), max(0, target - 2 * self.capacity + 1))
            self.model_layout_target = target
            self.scroll.get_vadjustment().set_value(desired_top * self.item_pitch())
            GLib.timeout_add(30, self.align_filled_model, self.store, target, desired_top, 0)
        self.model_pending_target = None
        self.model_pending_select = False
        self.model_pending_range = None
        self.model_selected_paths = set()
        self.view.handler_unblock(self.cursor_handler)
        if keep_focus:
            self.view.grab_focus()
        if self is self.window.active:
            self.selected()

    def align_filled_model(self, store, target, desired_top, attempts):
        if self.store is not store or self.model_layout_target != target:
            return False
        adjustment = self.scroll.get_vadjustment()
        desired = desired_top * self.item_pitch()
        if adjustment.get_upper() - adjustment.get_page_size() < desired - self.item_pitch():
            if attempts < 100:
                GLib.timeout_add(30, self.align_filled_model, store, target, desired_top, attempts + 1)
            else:
                self.model_layout_target = None
            return False
        adjustment.set_value(desired)
        position = self.store_position(target, desired_top)
        if position is not None:
            path = Gtk.TreePath.new_from_indices([position])
            self.view.handler_block(self.cursor_handler)
            self.view.set_cursor(path, None, False)
            self.view.handler_unblock(self.cursor_handler)
            self.view.scroll_to_path(path, False, 0.0, 0.0)
        self.model_layout_target = None
        return False

    def measure_item_geometry(self):
        if self.geometry_measured:
            return False
        self.geometry_attempts += 1
        if (self.view.get_realized() and len(self.store) > 2
                and self.scroll.get_vadjustment().get_value() < 1):
            first_found, first = self.view.get_cell_rect(Gtk.TreePath.new_from_indices([0]), None)
            next_found, next_row = self.view.get_cell_rect(Gtk.TreePath.new_from_indices([2]), None)
            if first_found and next_found:
                pitch = next_row.y - first.y
                if 10 <= pitch <= 80:
                    self.row_pitch = pitch
                    self.top_inset = first.y
                    self.geometry_measured = True
                    self.schedule_layout()
                    self.scroll.queue_draw()
                    return False
        return self.geometry_attempts < 80

    def name_color(self, _column, renderer, model, row, *_data):
        selected = self.view.path_is_selected(model.get_path(row))
        marked = model[row][3] in self.marked_paths
        if not model[row][3]:
            color = self.window.palette["background"]
        elif selected and self is self.window.active:
            color = self.window.palette["accent_text"]
        elif selected:
            color = self.window.palette["subtle"]
        elif marked:
            color = self.window.palette.get("marked", "#b58900")
        else:
            color = model[row][4]
        renderer.set_property("foreground", color)

    def prefix_color(self, _column, renderer, model, row, *_data):
        display, name, _size, path, color = model[row]
        marked = path in self.marked_paths
        prefix = display[0] if display != name else " "
        renderer.set_property("text", "*" if marked else prefix)
        selected = self.view.path_is_selected(model.get_path(row))
        if selected and self is self.window.active:
            foreground = self.window.palette["accent_text"]
        elif selected:
            foreground = self.window.palette["subtle"]
        elif marked:
            foreground = self.window.palette.get("marked", "#b58900")
        else:
            foreground = color or self.window.palette["background"]
        renderer.set_property("foreground", foreground)

    def draw_column_divider(self, widget, context):
        height = widget.get_allocated_height()
        visible_bottom = height
        # The clipped bottom row repeats the right column's first item.
        visible_range = self.view.get_visible_range()
        if visible_range is not None:
            _first, last = visible_range
            found, rect = self.view.get_cell_rect(last, None)
            if found and 0 <= rect.y < height:
                visible_bottom = max(0, min(height,
                    rect.y if rect.y + rect.height > height else rect.y + rect.height))
        background = Gdk.RGBA()
        background.parse(self.window.palette["background"])
        context.set_source_rgba(background.red, background.green, background.blue, background.alpha)
        context.rectangle(0, visible_bottom, self.view.get_allocated_width(), height - visible_bottom)
        context.fill()
        color = Gdk.RGBA()
        color.parse(self.window.palette["border"])
        context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        x = widget.get_allocated_width() / 2
        context.rectangle(round(x), 0, 2, height)
        context.fill()
        return False

    def resize_columns(self, _view, allocation):
        self.schedule_layout()

    def schedule_layout(self):
        if not self.layout_pending:
            self.layout_pending = True
            GLib.idle_add(self.apply_layout)

    def apply_layout(self):
        self.layout_pending = False
        width = max(120, (self.scroll.get_allocated_width() - 16) // 2)
        if self.view.get_item_width() != width:
            self.view.set_item_width(width)
            self.scroll.queue_draw()
            self.scroll_overlay.drawing_area.queue_draw()
        if self.entries and self.visible_capacity() != self.capacity:
            self.rebuild_entries(preserve=True)
        return False

    def selected(self, *_):
        if self.logical_to_stores and self.scroll.get_vadjustment().get_value() >= 0:
            top = self.visible_top()
            visible = {self.store_to_logical[position]
                       for position in range(2 * top, min(len(self.store), 2 * (top + self.capacity)))
                       if position in self.store_to_logical}
            selected_rows = [path.get_indices()[0] for path in self.view.get_selected_items()]
            selected_logicals = {self.store_to_logical[row] for row in selected_rows
                                 if row in self.store_to_logical}
            visible_selected = {self.store_to_logical[row] for row in selected_rows
                                if row in self.store_to_logical and top <= row // 2 < top + self.capacity}
            keep = (selected_logicals - visible) | visible_selected
            self.view.handler_block(self.cursor_handler)
            for logical in visible | selected_logicals:
                for position in self.logical_to_stores.get(logical, ()):
                    path = Gtk.TreePath.new_from_indices([position])
                    if logical in keep:
                        self.view.select_path(path)
                    else:
                        self.view.unselect_path(path)
            self.view.handler_unblock(self.cursor_handler)
        if _ and self.model_next_start < self.model_rows():
            self.model_selected_paths = {str(path) for path in self.selected_paths()}
            self.model_pending_target = None
            self.model_pending_select = False
            self.model_pending_range = None
        if _:
            self.pending_restore_name = None
        self.window.activate_pane(self, update=getattr(self.window, "active", self) is not self)
        logical = self.cursor_logical()
        if logical is not None:
            self.selection_anchor = logical
        selected = self.selected_paths()
        self.window.selection = selected[0] if selected else None
        if selected:
            item = next(path for path in self.view.get_selected_items()
                        if self.store[path][1] != "..")
            size = self.store[item][2]
            self.window.status.set_text(str(selected[0]) + (f" · {size}" if size else ""))
        else:
            self.window.status.set_text(str(self.path))
        self.window.refresh_file_footer(self)
        if self.index == 0 and getattr(self.window, "quick_enabled", False) and self.window.mode == "files":
            self.window.update_quick_view()

    def open(self, _view, treepath):
        if not self.store[treepath][3]:
            return
        path = Path(self.store[treepath][3])
        if str(path) in self.directory_paths:
            self.navigate(path)
        else:
            self.window.open_external(path)


class Window(Gtk.ApplicationWindow):
    @property
    def status(self):
        pane = getattr(self, "active", None)
        if pane is not None and self.mode in {"files", "gallery"}:
            return pane.status
        return self.global_status

    def __init__(self, app, args, state, client=None):
        super().__init__(application=app, title="Nightty")
        self.set_icon_name("pw.blackbird.SurfaceWorkspace")
        self.app, self.args, self.state = app, args, state
        self.appearance = load_appearance(getattr(args, "appearance", None))
        self.palette = self.appearance["palette"]
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="surface-io")
        self.listing_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="surface-listing")
        self.thumbnails_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="surface-thumbnails")
        self.transfers_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="surface-copy")
        self.jobs, self.terminals = [], []
        self.context, self.context_serial, self.preview_serial = None, 0, 0
        self.project_context_path = None
        self.selection, self.mode = None, "files"
        self.quick_enabled, self.quick_serial = False, 0
        self.inspector_target, self.inspector_return_mode = None, "files"
        self.agent_client = client or Agents(args.backend)
        self.agent_snapshot, self.agent_session = {}, None
        self.agent_loading, self.agent_sending, self.agent_request_key = False, False, None
        self.agent_drafts = {}
        self.pending_agent_sends = {}
        self.command_history = []
        self.command_history_index = None
        self.command_saved_input = ""
        self.command_terminal = None
        self.command_refresh_pending = False
        self.output_return_mode = "files"
        self.terminal_sequence = 0
        self.current_workspace_tab = None
        self.restoring_workspace_tab = False
        self.transfer_popover = None
        self.set_default_size(1400, 900)
        self.set_size_request(1500 if getattr(args, "smoke", False) else 760,
                              900 if getattr(args, "smoke", False) else 500)
        if getattr(args, "maximized", False) and not getattr(args, "smoke", False):
            self.maximize()
        self.connect("close-request", self.hide_window)
        connect_key(self, self.key, capture=True)
        self.gallery_key_capture = Gtk.EventControllerKey.new()
        self.add_controller(self.gallery_key_capture)
        self.gallery_key_capture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.gallery_key_capture.connect("key-pressed", self.capture_gallery_key)

        style = Gtk.CssProvider()
        style.load_from_data(stylesheet(self.appearance))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

        header = Gtk.HeaderBar(show_title_buttons=True)
        header.set_title_widget(Gtk.Label(label="Nightty"))
        self.tab_bar = Box(spacing=0)
        self.terminal_tab_bar = Box(spacing=0)
        self.tab_bar.pack_start(self.terminal_tab_bar, False, False, 0)
        header.pack_start(self.tab_bar)
        self.transfers_button = button("⇄", self.show_jobs)
        self.transfers_button.set_tooltip_text("Background transfers")
        header.pack_end(self.transfers_button)
        add_tab = button("+ Tab", self.new_terminal)
        add_tab.set_tooltip_text("New shell tab · Ctrl+Shift+T")
        header.pack_end(add_tab)
        self.set_titlebar(header)

        outer = Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        self.smoke_paintable = Gtk.WidgetPaintable.new(outer) if getattr(args, "smoke", False) else None
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.stack.set_vhomogeneous(False)
        outer.pack_start(self.stack, True, True, 0)
        left = args.left or state.data.get("left") or str(Path.cwd())
        right = args.right or state.data.get("right") or left
        self.initial_browser_state = {
            "paths": (Path(left), Path(right)),
            "pane_modes": ("files", "files"),
            "active": 0,
        }
        self.panes = []
        self.active = None
        self.file_panes = None
        self.pane_split_ratio = .5
        self.pane_split_width = 0
        self.pane_split_updating = False
        self.browser_state_tab = None
        self.gallery_flow = None

        project = Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.project_heading = label("Project", "heading")
        self.project_heading.set_margin_start(20)
        self.project_heading.set_margin_top(15)
        self.project_crumb = label("", "muted")
        self.project_crumb.set_margin_start(20)
        self.project_crumb.set_margin_bottom(12)
        project.pack_start(self.project_heading, False, False, 0)
        project.pack_start(self.project_crumb, False, False, 0)
        self.project_tabs = Gtk.Notebook()
        project.pack_start(self.project_tabs, True, True, 0)
        self.overview = textview()
        self.project_tabs.append_page(scrolled(self.overview), Gtk.Label(label="Context"))
        self.work_box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=20)
        self.project_tabs.append_page(scrolled(self.work_box), Gtk.Label(label="Work"))
        self.agents_box = self.build_agents()
        self.project_tabs.append_page(self.agents_box, Gtk.Label(label="Agents"))
        self.project_tabs.connect("switch-page", lambda _n, _p, number: self.refresh_agents() if number == 2 else None)
        self.stack.add_named(project, "project")

        inspector = Box(orientation=Gtk.Orientation.VERTICAL)
        inspector_toolbar = Box(spacing=10, margin=8)
        inspector_toolbar.pack_start(button("← Back  Esc", self.close_inspector), False, False, 0)
        self.inspector_name = label("", "heading")
        inspector_toolbar.pack_start(self.inspector_name, True, True, 0)
        inspector_toolbar.pack_end(button("Open  F4", self.open_external), False, False, 0)
        inspector.pack_start(inspector_toolbar, False, False, 0)
        inspector_body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        inspector.pack_start(inspector_body, True, True, 0)
        self.preview_stack = Gtk.Stack()
        self.document = textview()
        self.preview_stack.add_named(scrolled(self.document), "text")
        self.image = Gtk.Image()
        self.preview_stack.add_named(scrolled(self.image), "image")
        inspector_body.set_start_child(self.preview_stack)
        inspector_body.set_resize_start_child(True)
        inspector_body.set_shrink_start_child(False)
        details_box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        details_box.add_css_class("inspector-details")
        details_box.set_size_request(280, -1)
        details_box.pack_start(label("DETAILS", "section"), False, False, 0)
        self.inspector_details = label("", "muted")
        self.inspector_details.set_wrap(True)
        self.inspector_details.set_ellipsize(Pango.EllipsizeMode.NONE)
        details_box.pack_start(self.inspector_details, False, False, 0)
        inspector_body.set_end_child(scrolled(details_box))
        inspector_body.set_resize_end_child(False)
        inspector_body.set_shrink_end_child(False)
        inspector_body.set_position(1000)
        self.stack.add_named(inspector, "inspect")

        self.terminal_box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.terminal_tabs = Gtk.Notebook(scrollable=True)
        self.terminal_tabs.set_show_tabs(False)
        self.terminal_tabs.connect("switch-page", self.terminal_page_changed)
        self.terminal_box.pack_start(self.terminal_tabs, True, True, 0)
        self.stack.add_named(self.terminal_box, "console")

        self.info_box = Box()
        self.info_box.add_css_class("footer-box")
        self.global_status = label("", "footer-details")
        self.global_status.set_selectable(False)
        self.info_box.pack_start(self.global_status, True, True, 0)
        outer.pack_start(self.info_box, False, False, 0)
        self.command_row = Box(spacing=5)
        self.command_row.add_css_class("command-row")
        self.command_prompt = label("", "command-prompt")
        self.command_prompt.set_selectable(False)
        self.command_prompt.set_max_width_chars(70)
        self.command_row.pack_start(self.command_prompt, False, False, 0)
        self.command_entry = Gtk.Entry()
        self.command_entry.add_css_class("command-entry")
        self.command_entry.set_placeholder_text("")
        self.command_entry.set_tooltip_text("Run a shell command in the active pane's folder")
        self.command_entry.connect("activate", self.execute_command_line)
        connect_key(self.command_entry, self.command_entry_key)
        self.command_row.pack_start(self.command_entry, True, True, 0)
        outer.pack_start(self.command_row, False, False, 0)
        self.space_cache = {}
        self.update_command_prompt()
        self.show()
        self.route_browser_arrows(self)
        self.set_mode("console")
        GLib.timeout_add(350, self.update_transfers)
        GLib.timeout_add_seconds(1, self.refresh_tab_paths)

    def ensure_browser(self):
        if self.file_panes is not None:
            return
        browser = (self.current_workspace_tab.workspace_browser if self.current_workspace_tab is not None
                   else self.initial_browser_state)
        file_panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        file_panes.set_wide_handle(True)
        file_panes.add_css_class("file-split")
        on_resize(file_panes, self.resize_pane_split)
        file_panes.connect("notify::position", self.remember_pane_split)
        self.file_panes = file_panes
        self.stack.add_named(file_panes, "files")
        self.panes = [Pane(self, path, index) for index, path in enumerate(browser["paths"])]
        self.footer_height_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.VERTICAL)
        for pane in self.panes:
            self.footer_height_group.add_widget(pane.status_box)
        self.active = self.panes[browser["active"]]
        self.browser_state_tab = self.current_workspace_tab
        file_panes.set_start_child(self.panes[0])
        file_panes.set_resize_start_child(True)
        file_panes.set_shrink_start_child(True)
        self.right_stack = Gtk.Stack()
        self.right_stack.set_vhomogeneous(False)
        self.right_stack.add_named(self.panes[1], "files")
        quick = Box(orientation=Gtk.Orientation.VERTICAL)
        quick_toolbar = Box(spacing=8, margin=7)
        quick_toolbar.pack_start(label("QUICK VIEW", "section"), True, True, 0)
        self.quick_dest_button = button("Destination ▾", lambda: self.show_locations(self.panes[1], self.quick_dest_button))
        quick_toolbar.pack_end(button("Close", self.toggle_quick_view), False, False, 0)
        quick_toolbar.pack_end(self.quick_dest_button, False, False, 0)
        quick.pack_start(quick_toolbar, False, False, 0)
        self.quick_destination = label(str(self.panes[1].path), "muted")
        self.quick_destination.set_margin_start(10)
        quick.pack_start(self.quick_destination, False, False, 6)
        self.quick_title = label("Select a file in the left pane", "heading")
        self.quick_title.set_margin_start(10)
        quick.pack_start(self.quick_title, False, False, 8)
        self.quick_preview_stack = Gtk.Stack()
        self.quick_document = textview()
        self.quick_preview_stack.add_named(scrolled(self.quick_document), "text")
        self.quick_image = Gtk.Image()
        self.quick_preview_stack.add_named(scrolled(self.quick_image), "image")
        quick.pack_start(self.quick_preview_stack, True, True, 0)
        details_expander = Gtk.Expander(label="Details")
        self.quick_details = label("", "muted")
        self.quick_details.set_wrap(True)
        self.quick_details.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.quick_details.set_margin_start(12)
        details_expander.set_child(scrolled(self.quick_details))
        quick.pack_start(details_expander, False, False, 0)
        self.right_stack.add_named(quick, "quick")
        file_panes.set_end_child(self.right_stack)
        file_panes.set_resize_end_child(True)
        file_panes.set_shrink_end_child(True)
        # Files is created while the shell is visible. Size the hidden panes
        # from the live window so their first painted frame uses full columns.
        width = self.stack.get_allocated_width()
        if width > 1:
            usable = max(0, width - 6)
            left_width = round(usable * self.pane_split_ratio)
            file_panes.set_position(left_width)
            for pane, pane_width in zip(self.panes, (left_width, usable - left_width)):
                pane.view.set_item_width(max(120, (pane_width - 16) // 2))
        file_panes.show()
        self.route_browser_arrows(file_panes)
        self.activate_pane(self.active)
        for pane in self.panes:
            pane.navigate(pane.path)

    def ensure_gallery(self):
        self.ensure_browser()
        for pane in self.panes:
            if getattr(pane, "gallery_flow", None) is not None:
                continue
            pane.gallery_flow = Gtk.FlowBox()
            pane.gallery_flow.set_can_focus(True)
            pane.gallery_flow.set_focusable(True)
            pane.gallery_flow.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
            pane.gallery_flow.set_activate_on_single_click(False)
            pane.gallery_flow.set_row_spacing(8)
            pane.gallery_flow.set_column_spacing(8)
            pane.gallery_flow.set_min_children_per_line(1)
            pane.gallery_flow.set_max_children_per_line(2)
            pane.gallery_flow.set_homogeneous(True)
            pane.gallery_flow.set_valign(Gtk.Align.START)
            pane.gallery_flow.set_margin_top(7)
            pane.gallery_flow.set_margin_start(7)
            pane.gallery_flow.set_margin_end(7)
            pane.gallery_flow.set_margin_bottom(120)
            pane.gallery_flow.connect("selected-children-changed", self.gallery_selected)
            pane.gallery_flow.connect("child-activated", self.gallery_activated)
            connect_key(pane.gallery_flow, self.gallery_key)
            connect_focus(pane.gallery_flow, lambda _flow, _event, target=pane: self.activate_pane(target))
            connect_click(pane.gallery_flow, lambda flow, event, target=pane:
                          self.gallery_click(target, flow, event))
            pane.gallery_scroll = scrolled(pane.gallery_flow)
            on_resize(pane.gallery_scroll, lambda _scroll, _allocation, target=pane:
                      self.gallery_resized(target))
            pane.gallery_scroll.get_vadjustment().connect("value-changed", lambda _adjustment, target=pane:
                                                          self.gallery_scroll_changed(target))
            pane.gallery_overlay = drawing_overlay(pane.gallery_scroll,
                lambda scroll, context, target=pane: self.draw_gallery_edge(target, scroll, context))
            connect_click(pane.gallery_scroll, lambda scroll, event, target=pane:
                          self.gallery_click(target, scroll, event))
            pane.content_stack.add_named(pane.gallery_overlay, "gallery")
            pane.gallery_scroll.show()
        self.gallery_flow = self.active.gallery_flow

    def file_color(self, name, directory, symlink, executable):
        if symlink:
            return self.palette["symlink"]
        if directory:
            return self.palette["directory"]
        if executable:
            return self.palette["executable"]
        suffix = Path(name).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif", ".svg"}:
            return self.palette["image"]
        if suffix in {".mkv", ".mp4", ".mov", ".avi", ".webm", ".flv"}:
            return self.palette["video"]
        if suffix in {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aac"}:
            return self.palette["audio"]
        if suffix in {".pdf", ".doc", ".docx", ".odt", ".epub"}:
            return self.palette["document"]
        if suffix in {".py", ".go", ".js", ".ts", ".rs", ".c", ".h", ".sh", ".css", ".scss"}:
            return self.palette["source"]
        if suffix in {".json", ".yaml", ".yml", ".toml", ".csv", ".xml", ".sqlite", ".db"}:
            return self.palette["data"]
        if suffix in {".zip", ".gz", ".xz", ".bz2", ".7z", ".rar"}:
            return self.palette["archive"]
        if suffix in {".txt", ".md", ".rst", ".org", ".srt", ".log", ".html", ".htm"}:
            return self.palette["foreground"]
        return self.palette["unknown"]

    def resize_pane_split(self, paned, allocation):
        width = allocation.width
        if width < 2 or width == self.pane_split_width:
            return
        self.pane_split_width = width
        target = round(width * self.pane_split_ratio)
        if abs(paned.get_position() - target) > 1:
            self.pane_split_updating = True
            try:
                paned.set_position(target)
            finally:
                self.pane_split_updating = False

    def remember_pane_split(self, paned, _property):
        width = paned.get_allocated_width()
        position = paned.get_position()
        if (not self.pane_split_updating and width == self.pane_split_width
                and 0 < position < width):
            self.pane_split_ratio = position / width

    @staticmethod
    def size(value):
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    def async_call(self, operation, callback, error=None, executor=None):
        future = (executor or self.pool).submit(operation)

        def done(result):
            try:
                value = result.result()
            except Exception as exc:
                GLib.idle_add(lambda failure=exc: ((error or self.failure)(failure), False)[1])
            else:
                GLib.idle_add(lambda: (callback(value), False)[1])
        future.add_done_callback(done)

    def failure(self, error):
        self.status.set_text(str(error))

    def activate_pane(self, pane, update=True):
        if not hasattr(self, "panes"):
            return False
        changed = pane is not self.active
        self.active = pane
        if changed and self.mode in {"files", "gallery"}:
            self.mode = pane.content_stack.get_visible_child_name()
            self.state.save(mode=self.mode)
            if self.current_workspace_tab is not None and not self.restoring_workspace_tab:
                self.current_workspace_tab.workspace_mode = self.mode
            if self.mode == "gallery":
                self.gallery_flow = pane.gallery_flow
            self.refresh_mode_buttons()
        for candidate in self.panes:
            css = candidate.get_style_context()
            css.remove_class("pane-active")
            css.remove_class("pane-inactive")
            css.add_class("pane-active" if candidate is pane else "pane-inactive")
            candidate.view.queue_draw()
        if update:
            self.directory_changed(pane)
        if changed and self.mode in {"files", "gallery"}:
            selected = (self.gallery_selected_paths() if self.mode == "gallery"
                        else pane.selected_paths())
            self.selection = selected[0] if selected else None
        return False

    def refresh_mode_buttons(self):
        for terminal, _pid in self.terminals:
            control = getattr(terminal, "workspace_tab_button", None)
            if control is None:
                continue
            mode = (self.inspector_return_mode if self.mode == "inspect" else self.mode
                    ) if terminal is self.current_workspace_tab else terminal.workspace_mode
            if mode == "console":
                self.update_shell_cwd(terminal)
                folder = terminal.workspace_cwd
            elif (terminal is self.current_workspace_tab and self.active is not None
                  and self.browser_state_tab is terminal):
                folder = self.active.path
            else:
                browser = terminal.workspace_browser
                folder = browser["paths"][browser["active"]]
            folder = folder or terminal.workspace_browser["paths"][0]
            suffix = " · done" if terminal.workspace_exited else ""
            control.set_label(str(folder) + suffix)
            control.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            control.get_child().set_max_width_chars(24)
            control.set_tooltip_text(str(folder))
            css = control.get_style_context()
            css.add_class("top-tab-active") if terminal is self.current_workspace_tab else css.remove_class("top-tab-active")

    def refresh_tab_paths(self):
        if self.get_visible():
            self.refresh_mode_buttons()
        return True

    def terminal_page_changed(self, _notebook, _page, _number):
        self.refresh_mode_buttons()

    def select_terminal(self, index):
        if index < 0 or index >= len(self.terminals):
            return
        target = self.terminals[index][0]
        previous = self.current_workspace_tab
        if previous is not None and previous is not target:
            previous.workspace_mode = self.inspector_return_mode if self.mode == "inspect" else self.mode
            if self.browser_state_tab is previous:
                previous.workspace_browser = self.capture_browser_state()
        self.current_workspace_tab = target
        self.terminal_tabs.set_current_page(index)
        self.restoring_workspace_tab = True
        try:
            if previous is not target and target.workspace_mode in {"files", "gallery"}:
                self.restore_browser_state(target.workspace_browser)
                if self.panes:
                    self.browser_state_tab = target
            self.set_mode(target.workspace_mode)
        finally:
            self.restoring_workspace_tab = False
        self.refresh_mode_buttons()
        if target.workspace_mode == "console":
            target.grab_focus()

    def capture_browser_state(self):
        if not self.panes:
            return dict(self.initial_browser_state)
        return {
            "paths": tuple(pane.path for pane in self.panes),
            "pane_modes": tuple(pane.content_stack.get_visible_child_name() for pane in self.panes),
            "active": self.active.index,
        }

    def restore_browser_state(self, browser):
        if not self.panes:
            return
        for pane, path, mode in zip(self.panes, browser["paths"], browser["pane_modes"]):
            if pane.path != path:
                pane.navigate(path)
            if mode == "gallery":
                self.ensure_gallery()
            pane.content_stack.set_visible_child_name(mode)
            if mode == "gallery":
                self.show_gallery(pane)
        self.activate_pane(self.panes[browser["active"]], update=False)
        self.directory_changed(self.active)

    def switch_top_tab(self, direction):
        tabs = [terminal for terminal, _pid in self.terminals]
        if not tabs:
            return
        current = self.current_workspace_tab or tabs[0]
        target = tabs[(tabs.index(current) + direction) % len(tabs)]
        self.select_terminal(next(i for i, (terminal, _pid) in enumerate(self.terminals) if terminal is target))

    def top_tab_shortcut(self, key, state):
        if not state & Gdk.ModifierType.CONTROL_MASK or state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if key in {"Page_Up", "KP_Page_Up", "Prior", "KP_Prior"}:
            self.switch_top_tab(-1)
        elif key in {"Page_Down", "KP_Page_Down", "Next", "KP_Next"}:
            self.switch_top_tab(1)
        else:
            return False
        return True

    def route_browser_arrows(self, widget):
        if isinstance(widget, (Gtk.Button, Gtk.Label)):
            connect_key(widget, self.browser_arrow_from_control)
        for child in widget_children(widget):
            self.route_browser_arrows(child)

    def browser_arrow_from_control(self, _control, event):
        if self.mode not in {"files", "gallery"} or event.state & (Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        key = Gdk.keyval_name(event.keyval)
        if key not in {"Left", "Right", "Up", "Down", "Home", "End",
                       "KP_Left", "KP_Right", "KP_Up", "KP_Down", "KP_Home", "KP_End"}:
            return False
        if self.mode == "gallery":
            self.gallery_flow.grab_focus()
            return self.gallery_key(self.gallery_flow, event)
        self.active.view.grab_focus()
        return self.active.move_by_key(self.active.view, event)

    def directory_changed(self, pane):
        if not hasattr(self, "active") or not hasattr(self, "global_status"):
            return
        self.state.save(left=str(self.panes[0].path), right=str(self.panes[1].path))
        self.refresh_folder_footer(pane, request_space=True)
        self.refresh_file_footer(pane)
        if pane is not self.active:
            return
        self.update_command_prompt()
        self.refresh_mode_buttons()
        self.load_project_context(pane.path)

    def update_command_prompt(self):
        folder = (self.active.path if self.active is not None else
                  self.initial_browser_state["paths"][self.initial_browser_state["active"]])
        home = Path.home()
        try:
            relative = folder.relative_to(home)
            display = "~" if relative == Path(".") else f"~/{relative}"
        except ValueError:
            display = str(folder)
        user = os.environ.get("USER") or str(os.geteuid())
        host = socket.gethostname().split(".")[0]
        prompt = f"{user}@{host}:{display}{'#' if os.geteuid() == 0 else '$'}"
        self.command_prompt.set_text(prompt)
        self.command_prompt.set_tooltip_text(prompt)

    def refresh_folder_footer(self, pane, request_space=False):
        folder = str(pane.path)
        pane.folder_path.set_text(folder)
        pane.folder_path.set_tooltip_text(folder)
        space = self.space_cache.get(folder, "checking space…")
        pane.folder_stats.set_text(f"{pane.count.get_text()}  ·  {space}")
        if not request_space:
            return
        pane.space_serial += 1
        serial = pane.space_serial

        def inspect_space():
            filesystem = os.statvfs(folder)
            return filesystem.f_bavail * filesystem.f_frsize

        def shown(bytes_available):
            self.space_cache[folder] = f"{self.size(bytes_available)} free"
            if serial == pane.space_serial and str(pane.path) == folder:
                self.refresh_folder_footer(pane)

        def failed(_error):
            self.space_cache[folder] = "space unavailable"
            if serial == pane.space_serial and str(pane.path) == folder:
                self.refresh_folder_footer(pane)

        self.async_call(inspect_space, shown, failed)

    def refresh_file_footer(self, pane):
        pane_mode = pane.content_stack.get_visible_child_name()
        if pane_mode not in {"files", "gallery"}:
            return
        pane.footer_serial += 1
        serial = pane.footer_serial
        folder = pane.path
        if pane.count.get_text() == "Loading…":
            pane.status.set_text("Loading…")
            return
        if pane_mode == "files":
            selected = pane.marked_paths
            logical = pane.cursor_logical()
            row = pane.entries[logical] if logical is not None and logical < len(pane.entries) else None
        else:
            selected = {str(path) for path in self.gallery_selected_paths(pane)}
            selected_child = pane.gallery_flow.get_focus_child()
            current = (str(selected_child.file_path) if selected_child is not None
                       and hasattr(selected_child, "file_path") else None)
            row = next((entry for entry in pane.entries if entry[3] == current), None)
            if row is None and selected:
                row = next((entry for entry in pane.entries if entry[3] in selected), None)
        if len(selected) > 1:
            total = sum(pane.entry_sizes.get(path, 0) for path in selected)
            folders = len(selected & pane.directory_paths)
            noun = "item" if folders else "file"
            count = len(selected)
            suffix = " · folder contents excluded" if folders else ""
            pane.status.add_css_class("footer-marked")
            pane.status.set_text(f"{count} {noun}{'' if count == 1 else 's'} selected  ·  {self.size(total)}{suffix}")
            pane.status.set_tooltip_text("Folder contents are not included in the size" if folders else "")
            return
        if selected:
            selected_path = next(iter(selected))
            row = next((entry for entry in pane.entries if entry[3] == selected_path), row)
            pane.status.add_css_class("footer-marked")
        else:
            pane.status.remove_css_class("footer-marked")
        pane.status.set_tooltip_text("")
        if row is None:
            pane.status.set_text("No file selected")
            return
        if row[1] == "..":
            pane.status.set_text("..  ·  UP--DIR")
            return
        path = Path(row[3])
        kind = "<DIR>" if row[3] in pane.directory_paths else row[2] or "File"
        cached = pane.info_cache.get(row[3])

        def display(details=None):
            if details is None:
                pane.status.set_text(f"{row[1]}  ·  {kind}")
                return
            size, modified, mode, link = details
            size_text = "<DIR>" if stat.S_ISDIR(mode) else self.size(size)
            link_text = f"  ·  → {link}" if link else ""
            pane.status.set_text(
                f"{row[1]}  ·  {size_text}  ·  {datetime.fromtimestamp(modified):%Y-%m-%d %H:%M}"
                f"  ·  {stat.filemode(mode)}{link_text}")

        display(cached)
        if cached is not None:
            return

        def request():
            if (serial != pane.footer_serial or pane.path != folder
                    or pane.content_stack.get_visible_child_name() != pane_mode):
                return False

            def inspect():
                info = path.lstat()
                link = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
                return info.st_size, info.st_mtime, info.st_mode, link

            def shown(details):
                pane.info_cache[row[3]] = details
                if len(pane.info_cache) > 256:
                    pane.info_cache.pop(next(iter(pane.info_cache)))
                if (serial == pane.footer_serial and pane.path == folder
                        and pane.content_stack.get_visible_child_name() == pane_mode):
                    display(details)

            self.async_call(inspect, shown, lambda _exc: None)
            return False

        GLib.timeout_add(90, request)

    def load_project_context(self, path):
        if self.project_context_path == path and self.context is not None:
            return
        self.project_context_path = path
        self.selection = None
        self.context_serial += 1
        serial = self.context_serial

        def load():
            context = context_for(path)
            readme_text = None
            for filename in ("README.md", "INDEX.md"):
                entry = path / filename
                if entry.is_file():
                    try:
                        with entry.open() as stream:
                            readme_text = (filename, stream.read(100000))
                    except OSError:
                        readme_text = (filename, "Document unavailable")
                    break
            return context, readme_text

        def loaded(result):
            if serial != self.context_serial:
                return
            context, readme_text = result
            self.context = context
            self.project_heading.set_text(context["title"])
            crumbs = " / ".join(p["title"] for p in context["projects"]) or str(path)
            self.project_crumb.set_text(crumbs)
            lines = [context["title"], "=" * min(60, len(context["title"])), ""]
            for item in context["projects"]:
                if item["description"]:
                    lines += [item["title"], item["description"], ""]
            lines += ["Working folder", str(path), "", "Context is inherited from the project folders above.",
                      "Folder metadata does not change agent permissions or send files to an agent.", ""]
            if readme_text:
                lines += [readme_text[0], "─" * 40, readme_text[1]]
            lines.extend(context["warnings"])
            self.overview.get_buffer().set_text("\n".join(lines))
            for child in self.work_box.get_children():
                self.work_box.remove(child)
            for reference in context["work"]:
                self.work_box.pack_start(button(reference.name, lambda p=reference: self.preview(p)), False, False, 0)
                self.work_box.pack_start(label(str(reference), "muted"), False, False, 0)
            if not context["work"]:
                self.work_box.pack_start(label("No work documents linked from this folder."), False, False, 0)
            self.work_box.show()
            self.status.set_text(str(path))
        self.async_call(load, loaded)

    def build_locations_menu(self, pane):
        menu = Gtk.Popover()
        content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=8)
        favourites = self.state.favourites
        current = Path(os.path.abspath(os.fspath(pane.path)))
        recent = [path for path in self.state.recent_folders
                  if path != current and path not in favourites][:12]

        def section(title, paths):
            heading = label(title, "section")
            content.append(heading)
            if not paths:
                content.append(label("None yet", "muted"))
            for path in paths:
                content.append(button(str(path),
                    lambda target=path: (pane.navigate(target), menu.popdown())))

        section("Favourites", favourites)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        section("Recent folders", recent)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        content.append(button("★  Star this folder" if current not in favourites else "☆  Unstar this folder",
                              lambda: (pane.favourite(), menu.popdown())))
        menu.set_child(content)
        return menu

    def show_locations(self, pane, anchor=None):
        menu = self.build_locations_menu(pane)
        anchor = anchor or (self.quick_dest_button if pane.index == 1 and self.quick_enabled and self.mode == "files"
                            else pane.locations_button)
        previous = getattr(anchor, "nightty_popover", None)
        if previous is not None:
            previous.popdown()
            previous.unparent()
        anchor.nightty_popover = menu
        menu.set_parent(anchor)
        menu.popup()

    def add_favourite(self):
        self.state.toggle_favourite(self.active.path)

    def toggle_quick_view(self):
        if self.mode != "files":
            self.set_mode("files")
            if self.quick_enabled:
                self.activate_pane(self.panes[0])
                self.panes[0].view.grab_focus()
                self.update_quick_view()
                return
        self.quick_enabled = not self.quick_enabled
        self.right_stack.set_visible_child_name("quick" if self.quick_enabled else "files")
        self.activate_pane(self.panes[0])
        self.panes[0].view.grab_focus()
        if self.quick_enabled:
            self.quick_destination.set_text(str(self.panes[1].path))
            self.update_quick_view()
        else:
            self.quick_serial += 1

    def update_quick_view(self):
        self.quick_serial += 1
        serial = self.quick_serial
        selected = self.panes[0].selected_paths()
        path = selected[0] if selected else None
        if path is None or str(path) in self.panes[0].directory_paths:
            self.quick_title.set_text("Select a file in the left pane")
            self.quick_document.get_buffer().set_text("")
            self.quick_details.set_text("")
            self.quick_preview_stack.set_visible_child_name("text")
            return
        self.quick_title.set_text(path.name)
        self.quick_document.get_buffer().set_text("Loading preview…")
        self.quick_details.set_text("")
        self.quick_preview_stack.set_visible_child_name("text")

        def shown(value):
            if serial != self.quick_serial or not self.quick_enabled:
                return
            kind, result, details = value
            if kind == "image":
                self.quick_image.set_from_pixbuf(result)
            else:
                self.quick_document.get_buffer().set_text(result)
            self.quick_preview_stack.set_visible_child_name(kind)
            self.quick_details.set_text(details)

        self.async_call(lambda: self.read_preview(path, 650, 650), shown,
            lambda exc: self.quick_document.get_buffer().set_text(f"Cannot preview {path.name}: {exc}") if serial == self.quick_serial else None)

    def set_mode(self, mode):
        if mode not in {"files", "project", "console", "gallery", "inspect"}:
            mode = "files"
        previous_mode = self.mode
        if (mode in {"files", "gallery", "project"} and self.file_panes is None
                and self.current_workspace_tab is not None):
            terminal = self.current_workspace_tab
            self.update_shell_cwd(terminal)
            if terminal.workspace_cwd is not None:
                browser = terminal.workspace_browser
                paths = list(browser["paths"])
                paths[browser["active"]] = terminal.workspace_cwd
                browser["paths"] = tuple(paths)
        if mode == "gallery":
            self.ensure_gallery()
        elif mode == "files":
            self.ensure_browser()
        if (mode in {"files", "gallery"} and self.current_workspace_tab is not None
                and self.browser_state_tab is not self.current_workspace_tab):
            self.restore_browser_state(self.current_workspace_tab.workspace_browser)
            self.browser_state_tab = self.current_workspace_tab
        previous_pane_mode = (self.active.content_stack.get_visible_child_name()
                              if self.active is not None else "files")
        terminal = self.current_workspace_tab
        if mode == "console" and previous_mode != "console" and not self.restoring_workspace_tab:
            self.output_return_mode = previous_mode
            if terminal is not None:
                terminal.workspace_output_return_mode = previous_mode
        self.mode = mode
        if mode not in {"files", "gallery"}:
            self.status.remove_css_class("footer-marked")
        if mode != "inspect":
            self.state.save(mode=mode)
            if terminal is not None and not self.restoring_workspace_tab:
                terminal.workspace_mode = mode
        self.stack.set_visible_child_name("files" if mode in {"files", "gallery"} else mode)
        self.command_row.show() if mode in {"files", "gallery"} else self.command_row.hide()
        self.info_box.show() if mode in {"project", "inspect"} else self.info_box.hide()
        if mode == "project":
            browser = terminal.workspace_browser if terminal is not None else self.initial_browser_state
            self.load_project_context(self.active.path if self.browser_state_tab is terminal
                                      else browser["paths"][browser["active"]])
        if mode == "console":
            self.terminal_box.show()
            if not self.terminals:
                self.new_terminal(reveal=False)
            terminal = self.terminals[self.terminal_tabs.get_current_page()][0]
            terminal.grab_focus()
        else:
            if mode == "files":
                gallery_target = None
                if previous_pane_mode == "gallery":
                    flow = self.active.gallery_flow
                    focused = flow.get_focus_child()
                    selected_gallery = flow.get_selected_children()
                    item = (focused if focused is not None and hasattr(focused, "file_path")
                            else selected_gallery[-1] if selected_gallery else None)
                    gallery_target = item.file_path if item is not None else None
                self.active.content_stack.set_visible_child_name("files")
                if previous_pane_mode == "gallery":
                    self.active.gallery_serial += 1
                    self.active.focus_file_path(gallery_target)
                selected = self.active.selected_paths()
                self.selection = selected[0] if selected else None
                self.active.view.grab_focus()
                if self.command_refresh_pending:
                    self.command_refresh_pending = False
                    self.active.refresh()
                self.update_command_prompt()
                self.refresh_folder_footer(self.active)
                self.refresh_file_footer(self.active)
            elif mode == "gallery":
                if self.quick_enabled:
                    self.quick_enabled = False
                    self.quick_serial += 1
                    self.right_stack.set_visible_child_name("files")
                self.active.content_stack.set_visible_child_name("gallery")
                self.active.gallery_overlay.drawing_area.queue_draw()
                self.gallery_flow = self.active.gallery_flow
                if previous_pane_mode != "gallery":
                    self.show_gallery(self.active)
                else:
                    selected = self.gallery_selected_paths()
                    self.selection = selected[0] if selected else None
                self.update_command_prompt()
                self.refresh_folder_footer(self.active)
                self.refresh_file_footer(self.active)
                self.focus_active_browser()
        self.refresh_mode_buttons()
        if previous_mode != mode and mode in {"files", "gallery"}:
            GLib.idle_add(self.focus_browser_after_switch, mode)

    def focus_browser_after_switch(self, mode):
        if self.mode == mode and self.get_visible() and not text_has_focus(self.get_focus()):
            self.focus_active_browser()
        return False

    def focus_active_browser(self):
        if self.mode == "gallery":
            flow = self.active.gallery_flow
            selected = flow.get_selected_children()
            child = flow.get_focus_child() or (selected[-1] if selected else None)
            if (child is None or not child.get_visible()
                    or getattr(child, "is_placeholder", False)):
                child = (selected[-1] if selected else None) or next(
                    (item for item in widget_children(flow)
                     if not getattr(item, "is_placeholder", False)), None)
            if child is not None:
                if not selected:
                    flow.select_child(child)
                child.grab_focus()
            else:
                flow.grab_focus()
        else:
            self.active.view.grab_focus()

    def switch_browser_pane(self):
        self.activate_pane(self.panes[1 - self.active.index])
        self.focus_active_browser()

    def gallery_click(self, pane, widget, event, child=None):
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.activate_pane(pane)
        flow = pane.gallery_flow
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        top = self.gallery_top(pane)
        visible = [item for item in widget_children(flow)
                   if (hasattr(item, "gallery_index")
                       and top <= item.gallery_row < top + pane.gallery_capacity)]
        if child is not None and child not in visible:
            child = None
        if child is None:
            coordinates = widget.translate_coordinates(flow, int(event.x), int(event.y))
            if not visible:
                flow.grab_focus()
                return True
            x, y = coordinates if coordinates is not None else (0, 0)
            child = flow.get_child_at_pos(x, y)
            if child not in visible:
                def distance(item):
                    rectangle = item.get_allocation()
                    dx = max(rectangle.x - x, 0, x - rectangle.x - rectangle.width)
                    dy = max(rectangle.y - y, 0, y - rectangle.y - rectangle.height)
                    return dx * dx + dy * dy
                child = min(reversed(visible), key=distance)
        flow.unselect_all()
        flow.select_child(child)
        child.grab_focus()
        self.scroll_gallery_to(child)
        return True

    def gallery_selected(self, flow):
        if self.mode != "gallery" or flow is not self.gallery_flow:
            return
        if self.active.gallery_selection_syncing:
            return
        pane = self.active
        selected_children = [child for child in flow.get_selected_children()
                             if hasattr(child, "gallery_pane")]
        selected_indices = {child.gallery_index for child in selected_children}
        pane.gallery_selection_syncing = True
        try:
            for logical, replicas in pane.gallery_replicas_by_logical.items():
                for child in replicas:
                    if logical in selected_indices and not child.is_selected():
                        flow.select_child(child)
                    elif logical not in selected_indices and child.is_selected():
                        flow.unselect_child(child)
        finally:
            pane.gallery_selection_syncing = False
        paths = self.gallery_selected_paths()
        selected = {str(child.file_path) for child in selected_children}
        pane.view.handler_block(pane.cursor_handler)
        pane.view.unselect_all()
        for path in selected:
            for position in pane.path_to_stores.get(path, ()):
                pane.view.select_path(Gtk.TreePath.new_from_indices([position]))
        pane.view.handler_unblock(pane.cursor_handler)
        self.selection = paths[0] if paths else None
        self.refresh_file_footer(pane)

    def scroll_gallery_to(self, child):
        pane = child.gallery_pane
        flow = pane.gallery_flow
        if child.get_parent() is not flow:
            return False
        children = widget_children(flow)
        if not children or not hasattr(child, "gallery_row"):
            return False
        _columns, pitch, _first_y = self.gallery_geometry(flow, children)
        top = self.gallery_top(pane)
        if child.gallery_row < top:
            top = child.gallery_row
        elif child.gallery_row >= top + pane.gallery_capacity:
            top = child.gallery_row - pane.gallery_capacity + 1
        pane.gallery_scroll.get_vadjustment().set_value(min(self.gallery_max_top(pane), top) * pitch)
        return False

    @staticmethod
    def gallery_max_top(pane):
        return max(0, len(pane.gallery_children_by_logical) -
                   pane.gallery_columns * pane.gallery_capacity)

    def gallery_top(self, pane):
        children = widget_children(pane.gallery_flow)
        if len(children) < 2:
            return 0
        _columns, pitch, _first_y = self.gallery_geometry(pane.gallery_flow, children)
        return min(self.gallery_max_top(pane), max(0, round(
            pane.gallery_scroll.get_vadjustment().get_value() / pitch)))

    @staticmethod
    def gallery_child_for_index(pane, logical, top):
        replicas = pane.gallery_replicas_by_logical.get(logical, ())
        return next((child for child in replicas
                     if top <= child.gallery_row < top + pane.gallery_capacity),
                    min(replicas, key=lambda child: abs(child.gallery_row - top)) if replicas else None)

    @staticmethod
    def gallery_geometry(flow, children):
        first = children[0]
        first_y = first.get_allocation().y
        columns = 2 if len(children) > 1 and children[1].get_allocation().y == first_y else 1
        pitch = (children[columns].get_allocation().y - first_y if len(children) > columns
                 else 0)
        if pitch <= 0:
            pitch = first.get_allocated_height() + flow.get_row_spacing()
        return columns, pitch, first_y

    def gallery_visible_bottom(self, pane):
        flow = pane.gallery_flow
        children = widget_children(flow)
        if not children or children[0].get_allocated_height() <= 1:
            return pane.gallery_scroll.get_allocated_height()
        _columns, pitch, _first_y = self.gallery_geometry(flow, children)
        origin = children[0].translate_coordinates(pane.gallery_scroll, 0, 0)
        if origin is None:
            return pane.gallery_scroll.get_allocated_height()
        first_top = origin[1]
        viewport = pane.gallery_scroll.get_allocated_height()
        card_height = children[0].get_allocated_height()
        last_full_row = math.floor((viewport - first_top - card_height) / pitch)
        if last_full_row < 0:
            return 0
        return max(0, min(viewport, first_top + last_full_row * pitch + card_height))

    def draw_gallery_edge(self, pane, scroll, context):
        height = scroll.get_allocated_height()
        bottom = self.gallery_visible_bottom(pane)
        if bottom < height:
            background = Gdk.RGBA()
            background.parse(self.palette["background"])
            context.set_source_rgba(background.red, background.green, background.blue, background.alpha)
            context.rectangle(0, bottom, pane.gallery_flow.get_allocated_width(), height - bottom)
            context.fill()
        return False

    def gallery_scroll_changed(self, pane):
        if pane.gallery_scroll_snap_source:
            GLib.source_remove(pane.gallery_scroll_snap_source)
        pane.gallery_scroll_snap_source = GLib.timeout_add(45, self.snap_gallery_scroll, pane)
        pane.gallery_overlay.drawing_area.queue_draw()

    def snap_gallery_scroll(self, pane):
        pane.gallery_scroll_snap_source = 0
        children = widget_children(pane.gallery_flow)
        if len(children) > 1:
            _columns, pitch, _first_y = self.gallery_geometry(pane.gallery_flow, children)
            adjustment = pane.gallery_scroll.get_vadjustment()
            maximum = max(0, adjustment.get_upper() - adjustment.get_page_size())
            target = min(round(adjustment.get_value() / pitch), self.gallery_max_top(pane),
                         math.floor(maximum / pitch)) * pitch
            if abs(adjustment.get_value() - target) > 0.5:
                adjustment.set_value(target)
        return False

    def gallery_layout(self, pane):
        flow = pane.gallery_flow
        children = widget_children(flow)
        width = pane.gallery_scroll.get_allocated_width()
        columns = 2 if width >= 2 * 310 + flow.get_column_spacing() + 14 else 1
        card_height, pitch = 108, 108 + flow.get_row_spacing()
        first_y = flow.get_margin_top()
        if len(children) > 1 and children[0].get_allocated_height() > 1:
            _old_columns, measured_pitch, _first_y = self.gallery_geometry(flow, children)
            card_height = max(108, children[0].get_allocated_height())
            pitch = max(card_height + flow.get_row_spacing(), measured_pitch)
        viewport = pane.gallery_scroll.get_allocated_height()
        capacity = max(1, math.floor((viewport - first_y - card_height) / pitch) + 1)
        return columns, capacity

    def gallery_resized(self, pane):
        if pane.content_stack.get_visible_child_name() != "gallery":
            return
        if pane.gallery_build_waiting and pane.gallery_scroll.get_allocated_width() > 1:
            self.show_gallery(pane)
            return
        if pane.gallery_resize_source:
            GLib.source_remove(pane.gallery_resize_source)
        pane.gallery_resize_source = GLib.timeout_add(120, self.apply_gallery_resize, pane)

    def apply_gallery_resize(self, pane):
        pane.gallery_resize_source = 0
        if (not pane.gallery_building
                and pane.content_stack.get_visible_child_name() == "gallery"
                and widget_children(pane.gallery_flow)
                and self.gallery_layout(pane) != (pane.gallery_columns, pane.gallery_capacity)):
            self.show_gallery(pane)
        return False

    def gallery_selected_paths(self, pane=None):
        flow = (pane or self.active).gallery_flow
        paths = []
        seen = set()
        for child in sorted(flow.get_selected_children(),
                            key=lambda item: getattr(item, "gallery_index", -1)):
            if (hasattr(child, "gallery_index") and not child.is_parent
                    and child.file_path not in seen):
                paths.append(child.file_path)
                seen.add(child.file_path)
        return paths

    def capture_gallery_key(self, _controller, keyval, _keycode, state):
        focus = self.get_focus()
        key = Gdk.keyval_name(keyval)
        if self.top_tab_shortcut(key, state):
            return True
        if (self.mode == "files" and key in {"Insert", "KP_Insert"}
                and not state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK
                               | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)):
            self.active.view.grab_focus()
            self.active.toggle_mark_current()
            return True
        if (self.mode in {"files", "gallery"} and key in {"Tab", "ISO_Left_Tab"}
                and not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                               | Gdk.ModifierType.SUPER_MASK)
                and not isinstance(focus, Vte.Terminal)):
            if self.mode == "files" and self.quick_enabled:
                self.toggle_quick_view()
            self.switch_browser_pane()
            return True
        if (self.mode == "files" and key in {"Home", "End", "KP_Home", "KP_End"}
                and not text_has_focus(focus)):
            self.active.view.grab_focus()
            return self.active.move_by_key(self.active.view, argparse.Namespace(keyval=keyval, state=state))
        if self.mode != "gallery" or text_has_focus(focus):
            return False
        if key not in {"Left", "Right", "Up", "Down", "Home", "End",
                       "KP_Left", "KP_Right", "KP_Up", "KP_Down", "KP_Home", "KP_End"}:
            return False
        return self.gallery_key(self.active.gallery_flow, argparse.Namespace(keyval=keyval, state=state))

    def gallery_key(self, flow, event):
        if self.mode != "gallery" or flow is not self.active.gallery_flow or event.state & (Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        key = Gdk.keyval_name(event.keyval)
        if key in {"Tab", "ISO_Left_Tab"}:
            self.switch_browser_pane()
            return True
        if key not in {"Left", "Right", "Up", "Down", "Home", "End",
                       "KP_Left", "KP_Right", "KP_Up", "KP_Down", "KP_Home", "KP_End"}:
            return self.command_key(event)
        children = self.active.gallery_children_by_logical
        if not children:
            return True
        current = flow.get_focus_child()
        if current not in children:
            selected = flow.get_selected_children()
            current = selected[-1] if selected else next((item for item in children if item is not None), None)
        if current is None:
            if key.removeprefix("KP_") in {"Home", "End"}:
                self.active.gallery_pending_target = 0 if key.removeprefix("KP_") == "Home" else len(children) - 1
            return True
        index = current.gallery_index
        current_layout = (self.active.gallery_columns, self.active.gallery_capacity)
        new_layout = self.gallery_layout(self.active)
        capacity = new_layout[1]
        top = self.gallery_top(self.active)
        direction = key.removeprefix("KP_")
        self.active.gallery_pending_target = None
        if direction == "Home":
            target_index = 0
        elif direction == "End":
            target_index = len(children) - 1
        elif direction == "Right":
            target_index = min(index + capacity, len(children) - 1)
        elif direction == "Left":
            target_index = index - capacity if index >= capacity else 0
        elif direction == "Down":
            target_index = min(index + 1, len(children) - 1)
        else:
            target_index = max(index - 1, 0)
        if direction == "Home":
            next_top = 0
        elif direction == "End":
            next_top = self.gallery_max_top(self.active)
        elif target_index < top:
            next_top = max(0, min(target_index, top - capacity))
        elif target_index >= top + self.active.gallery_columns * capacity:
            next_top = min(self.gallery_max_top(self.active),
                           max(top + capacity, target_index - self.active.gallery_columns * capacity + 1))
        else:
            next_top = top
        if new_layout != current_layout:
            self.show_gallery(self.active)
            self.active.gallery_pending_target = target_index
            self.active.gallery_pending_top = next_top
            return True
        self.active.gallery_scroll.get_vadjustment().set_value(
            next_top * self.gallery_geometry(flow, widget_children(flow))[1])
        target = self.gallery_child_for_index(self.active, target_index, next_top)
        if target is None:
            self.active.gallery_pending_target = target_index
            return True
        if not event.state & Gdk.ModifierType.SHIFT_MASK:
            flow.unselect_all()
        flow.select_child(target)
        target.grab_focus()
        self.scroll_gallery_to(target)
        return True

    def gallery_activated(self, _flow, child):
        if self.mode != "gallery" or _flow is not self.active.gallery_flow:
            return
        if getattr(child, "is_placeholder", False):
            return
        if child.is_directory:
            self.active.navigate(child.file_path)
        else:
            self.open_external(child.file_path)

    def show_gallery(self, pane=None):
        pane = pane or self.active
        if pane.gallery_resize_source:
            GLib.source_remove(pane.gallery_resize_source)
            pane.gallery_resize_source = 0
        if pane.gallery_scroll.get_allocated_width() <= 1:
            pane.gallery_build_waiting = True
            return
        pane.gallery_build_waiting = False
        same_folder = pane.gallery_displayed_path == pane.path
        previous_top = self.gallery_top(pane) if same_folder else 0
        pane.gallery_columns, pane.gallery_capacity = self.gallery_layout(pane)
        pane.gallery_serial += 1
        serial = pane.gallery_serial
        pane.gallery_building = True
        flow = pane.gallery_flow
        old_children = widget_children(flow)
        focus = self.get_focus()
        focused_path = focus.file_path if focus in old_children and hasattr(focus, "file_path") else None
        should_focus_gallery = (pane is self.active and self.mode == "gallery"
                                and (not same_folder or focus is flow or focus in old_children))
        flow.set_min_children_per_line(pane.gallery_columns)
        flow.set_max_children_per_line(pane.gallery_columns)
        path = pane.path
        selected_gallery = ([child.file_path for child in flow.get_selected_children()
                             if hasattr(child, "file_path")]
                            if same_folder else [])
        restore_paths = set(selected_gallery) if selected_gallery else set(pane.selected_paths())
        pane.gallery_displayed_path = path
        pane.gallery_children_by_logical = []
        pane.gallery_replicas_by_logical = {}
        pane.gallery_preview_results = {}
        pane.gallery_render_waiters = {}
        pane.gallery_pending_target = None
        pane.gallery_pending_top = None
        for child in old_children:
            flow.remove(child)
        if should_focus_gallery:
            flow.grab_focus()
        if not same_folder:
            pane.gallery_scroll.get_vadjustment().set_value(0)
        pane.status.set_text("Loading previews…")
        if pane is self.active and self.mode == "gallery":
            self.gallery_flow = flow
            self.selection = None

        def loaded(rows):
            if serial != pane.gallery_serial:
                return
            entries = [("..", True, 0, path.parent, True)] if path.parent != path else []
            entries += [(name, directory, size, path / name, False)
                        for name, directory, _symlink, size, _executable in rows]
            restore_paths.intersection_update(entry[3] for entry in entries)
            pane.gallery_children_by_logical = [None] * len(entries)
            ordered = []
            total_rows = (min(len(entries), max(pane.gallery_capacity,
                          len(entries) - pane.gallery_capacity))
                          if pane.gallery_columns == 2 else len(entries))
            for row in range(total_rows):
                for column in range(pane.gallery_columns):
                    logical = row + column * pane.gallery_capacity
                    ordered.append((row, logical, entries[logical])
                                   if logical < len(entries) else (row, None, None))
            position = 0

            def add_batch():
                nonlocal position
                if serial != pane.gallery_serial:
                    return False
                for row, logical, entry in ordered[position:position + 40]:
                    if logical is None:
                        self.add_gallery_placeholder(pane)
                        continue
                    name, directory, size, target, parent = entry
                    child = self.add_gallery_tile(pane, name, directory, size, target, parent, serial)
                    child.gallery_index = logical
                    child.gallery_row = row
                    if pane.gallery_children_by_logical[logical] is None:
                        pane.gallery_children_by_logical[logical] = child
                    pane.gallery_replicas_by_logical.setdefault(logical, []).append(child)
                    if target in restore_paths:
                        flow.select_child(child)
                        if (should_focus_gallery and (focused_path is None or focused_path == target)
                                and previous_top <= row < previous_top + pane.gallery_capacity):
                            child.grab_focus()
                    elif (not restore_paths and logical == 0
                          and pane is self.active and self.mode == "gallery"):
                        flow.select_child(child)
                        if should_focus_gallery:
                            child.grab_focus()
                    if logical == pane.gallery_pending_target:
                        target_top = pane.gallery_pending_top
                        if target_top is not None and target_top <= row < target_top + pane.gallery_capacity:
                            flow.unselect_all()
                            flow.select_child(child)
                            if pane is self.active and self.mode == "gallery":
                                child.grab_focus()
                            self.scroll_gallery_to(child)
                            pane.gallery_pending_target = None
                position += 40
                if position >= len(ordered):
                    pane.gallery_building = False
                    flow.queue_resize()
                    pane.gallery_scroll.queue_resize()
                    pane.gallery_overlay.drawing_area.queue_draw()
                    _columns, pitch, _first_y = self.gallery_geometry(flow, widget_children(flow)) if widget_children(flow) else (1, 1, 0)
                    pane.gallery_scroll.get_vadjustment().set_value(
                        min(pane.gallery_pending_top if pane.gallery_pending_top is not None else previous_top,
                            self.gallery_max_top(pane)) * pitch)
                    selected = flow.get_selected_children()
                    if selected:
                        focused = next((item for item in selected if item.file_path == focused_path), selected[0])
                        target = self.gallery_child_for_index(pane, focused.gallery_index, previous_top)
                        if target is not None:
                            self.scroll_gallery_to(target)
                            GLib.timeout_add(60, lambda: (self.scroll_gallery_to(target), False)[1]
                                             if serial == pane.gallery_serial else False)
                    if should_focus_gallery:
                        if selected and target is not None:
                            target.grab_focus()
                        else:
                            flow.grab_focus()
                    elif focus is not None and focus.get_root() is self:
                        focus.grab_focus()
                    pane.gallery_pending_top = None
                    self.gallery_resized(pane)
                    self.refresh_file_footer(pane)
                    return False
                return True

            GLib.idle_add(add_batch)

        loaded(pane.listed_rows)

    @staticmethod
    def add_gallery_placeholder(pane):
        child = Gtk.FlowBoxChild()
        child.is_placeholder = True
        child.set_size_request(310, 108)
        child.set_can_focus(False)
        child.set_sensitive(False)
        pane.gallery_flow.insert(child, -1)
        child.show()

    def add_gallery_tile(self, pane, name, directory, size, path, parent, serial):
        child = Gtk.FlowBoxChild()
        child.gallery_pane = pane
        child.file_path, child.is_directory, child.is_parent = path, directory, parent
        connect_key(child, lambda _child, event, flow=pane.gallery_flow: self.gallery_key(flow, event))
        connect_click(child, lambda item, event: self.gallery_click(pane, item, event, item))
        child.set_size_request(310, 108)
        if parent:
            parent_tile = Box(orientation=Gtk.Orientation.VERTICAL)
            parent_tile.add_css_class("gallery-tile")
            parent_label = Gtk.Label(label="..", xalign=0.5, yalign=0.5)
            parent_label.add_css_class("gallery-parent")
            parent_label.set_tooltip_text("Parent directory")
            parent_tile.pack_start(parent_label, True, True, 0)
            child.set_child(parent_tile)
            pane.gallery_flow.insert(child, -1)
            child.show()
            return child
        tile = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tile.add_css_class("gallery-tile")
        preview = Gtk.Stack()
        preview.set_size_request(92, 80)
        if directory:
            image = Gtk.Image.new_from_icon_name("folder")
        else:
            guessed_type = Gio.content_type_guess(str(path), None)[0] or "application/octet-stream"
            type_icon = Gio.content_type_get_icon(guessed_type)
            image = (Gtk.Image.new_from_gicon(type_icon) if type_icon
                     else Gtk.Image.new_from_icon_name("text-x-generic"))
        image.set_pixel_size(64)
        preview.add_named(image, "image")
        child.preview_image = image
        excerpt = Gtk.Label(xalign=0, yalign=0)
        excerpt.set_size_request(92, 80)
        excerpt.set_wrap(True)
        excerpt.add_css_class("gallery-excerpt")
        preview.add_named(excerpt, "text")
        tile.pack_start(preview, False, False, 0)
        details = Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        details.set_hexpand(True)
        name_label = Gtk.Label(label=name, xalign=0)
        name_label.add_css_class("gallery-name")
        if directory:
            name_label.add_css_class("gallery-folder-name")
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_max_width_chars(22)
        name_label.set_tooltip_text(str(path))
        details.pack_start(name_label, False, False, 0)
        kind_label = Gtk.Label(label="Folder" if directory else self.size(size), xalign=0)
        kind_label.add_css_class("muted")
        kind_label.set_ellipsize(Pango.EllipsizeMode.END)
        details.pack_start(kind_label, False, False, 0)
        extra_label = Gtk.Label(xalign=0)
        extra_label.add_css_class("muted")
        extra_label.set_ellipsize(Pango.EllipsizeMode.END)
        details.pack_start(extra_label, False, False, 0)
        tile.pack_start(details, True, True, 0)
        child.set_child(tile)
        pane.gallery_flow.insert(child, -1)
        child.show()

        def render():
            if serial != pane.gallery_serial:
                return None
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if directory:
                return None, None, "Folder", modified
            kind = Gio.content_type_guess(str(path), None)[0] or ""
            description = Gio.content_type_get_description(kind) or "File"
            summary = f"{description} · {self.size(size)}"
            if kind.startswith("image/"):
                try:
                    _format, width, height = GdkPixbuf.Pixbuf.get_file_info(str(path))
                    dimensions = f"{width} × {height} px" if width and height else modified
                    return "image", GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 92, 80, True), summary, dimensions
                except Exception:
                    return "icon", kind, summary, modified
            if kind == "application/pdf":
                try:
                    return "image", pdf_pixbuf(path, 80), summary, modified
                except Exception:
                    return "icon", kind, summary, modified
            if not (kind.startswith("text/") or kind in {
                    "application/json", "application/xml", "application/javascript",
                    "application/x-yaml", "application/toml"}):
                return "icon", kind, summary, modified
            with path.open("rb") as stream:
                content = stream.read(350)
            if b"\0" not in content:
                words = " ".join(content.decode("utf-8", errors="replace").split())
                if words:
                    return "text", "\n".join(textwrap.wrap(words, width=14)[:3]), summary, modified
            return "icon", kind, summary, modified

        def shown(result):
            if serial != pane.gallery_serial or result is None:
                return
            kind, value, summary, extra = result
            kind_label.set_text(summary)
            extra_label.set_text(extra)
            if kind == "image":
                image.set_from_pixbuf(value)
            elif kind == "text":
                excerpt.set_text(value)
                preview.set_visible_child_name("text")
            elif kind == "icon":
                image.set_from_gicon(Gio.content_type_get_icon(value))
                image.set_pixel_size(64)

        if path in pane.gallery_preview_results:
            shown(pane.gallery_preview_results[path])
        elif path in pane.gallery_render_waiters:
            pane.gallery_render_waiters[path].append(shown)
        else:
            pane.gallery_render_waiters[path] = [shown]

            def delivered(result):
                if serial != pane.gallery_serial:
                    return
                pane.gallery_preview_results[path] = result
                for callback in pane.gallery_render_waiters.pop(path, ()):
                    callback(result)

            def failed(_exc):
                if serial == pane.gallery_serial:
                    pane.gallery_render_waiters.pop(path, None)

            self.async_call(render, delivered, failed, executor=self.thumbnails_pool)
        return child

    def read_preview(self, path, width, height):
        info = path.stat()
        content_type = Gio.content_type_guess(str(path), None)[0] or "application/octet-stream"
        description = Gio.content_type_get_description(content_type) or content_type
        details = ["Name", path.name, "", "Type", description, content_type,
                   "", "Size", f"{self.size(info.st_size)} ({info.st_size:,} bytes)",
                   "", "Modified", datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                   "", "Permissions", stat.filemode(info.st_mode),
                   "", "Location", str(path.parent)]
        if path.is_symlink():
            details += ["", "Link target", os.readlink(path)]
        try:
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

    def preview(self, path=None):
        path = path or self.selection
        if path is None or str(path) in self.active.directory_paths:
            return
        path = Path(path)
        if self.mode != "inspect":
            self.inspector_return_mode = self.mode
        self.inspector_target = path
        self.preview_serial += 1
        serial = self.preview_serial
        self.inspector_name.set_text(path.name)
        self.inspector_details.set_text("Reading file details…")
        self.document.get_buffer().set_text("Loading preview…")
        self.preview_stack.set_visible_child_name("text")
        self.set_mode("inspect")

        def show(value):
            if serial != self.preview_serial:
                return
            kind, result, details = value
            if kind == "image":
                self.image.set_from_pixbuf(result)
            else:
                self.document.get_buffer().set_text(result)
            self.preview_stack.set_visible_child_name(kind)
            self.inspector_details.set_text(details)
            self.status.set_text(str(path))
        self.async_call(lambda: self.read_preview(path, 1200, 860), show,
            lambda exc: self.document.get_buffer().set_text(f"Cannot inspect {path.name}: {exc}") if serial == self.preview_serial else None)

    def close_inspector(self):
        self.set_mode(self.inspector_return_mode)
        self.inspector_target = None

    def open_external(self, path=None):
        path = Path(path or (self.inspector_target if self.mode == "inspect" else self.selection or self.active.path))
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except GLib.Error as exc:
            self.status.set_text(f"Cannot open {path.name}: {exc}")

    def copy(self):
        sources = self.gallery_selected_paths() if self.mode == "gallery" else self.active.operation_paths()
        if not sources:
            self.status.set_text("Select a file or mark files to copy (Insert or Ctrl+T).")
            return
        destination = self.panes[1 - self.active.index].path
        source_label = ("marked" if self.mode == "files" and self.active.marked_paths
                        else "current" if self.mode == "files" and len(sources) == 1
                        else "selected")
        confirm(self, f"Copy {len(sources)} {source_label} item(s) in the background?",
                str(destination) + "\nExisting destinations will be left unchanged.",
                lambda: self.start_copy(sources, destination))

    def start_copy(self, sources, destination):
        job = Transfer(sources, destination)
        self.jobs.append(job)
        self.transfers_pool.submit(job.run)
        self.status.set_text("Copy queued; you can keep navigating.")
        if self.mode == "files":
            def restore_footer():
                if self.mode == "files":
                    self.refresh_file_footer(self.active)
                return False

            GLib.timeout_add_seconds(2, restore_footer)
        return job

    def update_transfers(self):
        running = [job for job in self.jobs if job.status in {"queued", "copying"}]
        self.transfers_button.set_label(f"⇄ {len(running)}" if running else "⇄")
        for job in self.jobs:
            if job.status in {"completed", "failed", "cancelled"} and not getattr(job, "announced", False):
                job.announced = True
                for pane in self.panes:
                    if os.path.abspath(os.fspath(pane.path)) == os.path.abspath(os.fspath(job.destination)):
                        pane.navigate(pane.path)
                if self.mode != "files":
                    self.status.set_text(f"Copy {job.status}: {job.files} files, {self.size(job.bytes)}" + (f" · {job.error}" if job.error else ""))
        if running:
            progress = f"{len(running)} transfer(s) · " + " · ".join(
                f"{job.files} files / {self.size(job.bytes)}" for job in running)
            self.transfers_button.set_tooltip_text(progress)
            if self.mode != "files":
                self.status.set_text(progress)
        else:
            self.transfers_button.set_tooltip_text("Background transfers")
        if hasattr(self, "jobs_label") and self.jobs_label.get_parent():
            self.jobs_label.set_text("\n\n".join(
                f"{job.status.upper()}   {job.files} files · {self.size(job.bytes)}\n{job.destination}\n{job.error}" for job in self.jobs) or "No transfers yet.")
            self.cancel_transfers_button.set_sensitive(bool(running))
        return True

    def show_jobs(self):
        if self.transfer_popover is None:
            self.transfer_popover = Gtk.Popover()
            self.transfer_popover.set_parent(self.transfers_button)
            content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
            content.pack_start(label("BACKGROUND TRANSFERS", "section"), False, False, 0)
            self.jobs_label = label("")
            self.jobs_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            self.jobs_label.set_wrap(True)
            jobs_scroll = scrolled(self.jobs_label)
            jobs_scroll.set_size_request(360, 180)
            content.pack_start(jobs_scroll, True, True, 0)
            self.cancel_transfers_button = button("Cancel running transfers", lambda:
                [job.cancel.set() for job in self.jobs if job.status in {"queued", "copying"}])
            content.pack_start(self.cancel_transfers_button, False, False, 0)
            self.transfer_popover.set_child(content)
        if self.transfer_popover.get_visible():
            self.transfer_popover.popdown()
            return
        self.update_transfers()
        self.transfer_popover.show()
        self.transfer_popover.popup()

    def command_key(self, event):
        modifiers = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                     | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK)
        if self.mode not in {"files", "gallery"} or event.state & modifiers:
            return False
        if Gdk.keyval_name(event.keyval) == "BackSpace":
            self.command_entry.set_text(self.command_entry.get_text()[:-1])
            self.command_entry.set_position(-1)
            return True
        codepoint = Gdk.keyval_to_unicode(event.keyval)
        if not codepoint:
            return False
        character = chr(codepoint)
        if not character.isprintable() or character == " ":
            return False
        self.command_entry.set_text(self.command_entry.get_text() + character)
        self.command_entry.grab_focus()
        self.command_entry.set_position(-1)
        return True

    def command_entry_key(self, _entry, event):
        key = Gdk.keyval_name(event.keyval)
        if (event.state & Gdk.ModifierType.CONTROL_MASK
                and not event.state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK)
                and self.mode_shortcut(key)):
            return True
        if key == "Escape":
            self.command_entry.set_text("")
            self.command_history_index = None
            self.focus_active_browser()
            return True
        if key == "Up" and self.command_history:
            if self.command_history_index is None:
                self.command_saved_input = self.command_entry.get_text()
                self.command_history_index = len(self.command_history) - 1
            else:
                self.command_history_index = max(0, self.command_history_index - 1)
            self.command_entry.set_text(self.command_history[self.command_history_index])
            self.command_entry.set_position(-1)
            return True
        if key == "Down" and self.command_history_index is not None:
            self.command_history_index += 1
            if self.command_history_index >= len(self.command_history):
                self.command_history_index = None
                value = self.command_saved_input
            else:
                value = self.command_history[self.command_history_index]
            self.command_entry.set_text(value)
            self.command_entry.set_position(-1)
            return True
        return False

    def change_directory_from_command(self, command):
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        if not words or words[0] != "cd":
            return False
        if len(words) == 1:
            name = str(Path.home())
        elif len(words) == 2:
            name = words[1]
        elif len(words) == 3 and words[1] == "--":
            name = words[2]
        else:
            return False
        if name == "-":
            target = self.active.previous_path
            if target is None:
                self.status.set_text("cd: no previous directory")
                return True
        else:
            target = Path(os.path.expandvars(name)).expanduser()
            if not target.is_absolute():
                target = self.active.path / target
        target = Path(os.path.normpath(target.absolute()))
        pane = self.active
        previous_directory = pane.path

        def checked(is_directory):
            if pane is not self.active or pane.path != previous_directory:
                return
            if not is_directory:
                self.status.set_text(f"cd: not a directory: {target}")
                return
            pane.navigate(target)
            terminal = self.shell_for_commands()
            if self.terminal_has_foreground_program(terminal):
                terminal.workspace_cwd = None
                self.status.set_text("Folder changed; the shell is busy and will sync at its next command.")
                if self.mode != "gallery" or target == previous_directory:
                    self.focus_active_browser()
                return
            if terminal.workspace_cwd != target:
                natural_command = name != "-" and "$" not in command and "~" not in command
                shell_command = (command if natural_command and terminal.workspace_cwd == previous_directory
                                 else f"cd {shlex.quote(str(target))}")
                terminal.workspace_pending.append(shell_command + "\n")
                terminal.workspace_cwd = target
                self.flush_terminal_commands(terminal)
            self.terminal_tabs.set_current_page(terminal.workspace_page_index)
            if self.mode != "gallery" or target == previous_directory:
                self.focus_active_browser()

        if str(target) in pane.directory_paths or target == pane.path or target == pane.path.parent:
            checked(True)
        else:
            self.status.set_text(f"Checking {target}…")
            self.async_call(target.is_dir, checked,
                lambda exc: self.status.set_text(f"cd: cannot check {target}: {exc}")
                if pane is self.active and pane.path == previous_directory else None)
        return True

    def execute_command_line(self, _entry=None):
        command = self.command_entry.get_text().strip()
        if not command:
            return
        if not self.command_history or self.command_history[-1] != command:
            self.command_history.append(command)
        self.command_history_index = None
        self.command_saved_input = ""
        self.command_entry.set_text("")
        if self.change_directory_from_command(command):
            return
        directory = self.active.path
        terminal = self.shell_for_commands()
        if self.terminal_has_foreground_program(terminal):
            self.command_entry.set_text(command)
            self.status.set_text("Shell is busy; wait for the prompt or use its terminal view.")
            return
        payload = (f"cd {shlex.quote(str(directory))} && {command}\n"
                   if terminal.workspace_cwd != directory else command + "\n")
        terminal.workspace_pending.append(payload)
        terminal.workspace_cwd = directory
        self.flush_terminal_commands(terminal)
        self.command_refresh_pending = True
        self.set_mode("console")
        self.terminal_tabs.set_current_page(terminal.workspace_page_index)
        terminal.grab_focus()

    def shell_for_commands(self):
        terminal = self.current_workspace_tab
        if terminal is None or terminal.workspace_exited:
            terminal = self.new_terminal()
        self.update_shell_cwd(terminal)
        self.command_terminal = terminal
        return terminal

    def update_shell_cwd(self, terminal):
        if (not terminal.workspace_ready or terminal.workspace_pending
                or self.terminal_has_foreground_program(terminal)):
            return
        record = next((record for record in self.terminals if record[0] is terminal), None)
        if record is None or not record[1]:
            return
        try:
            terminal.workspace_cwd = Path(os.readlink(f"/proc/{record[1]}/cwd"))
        except OSError:
            pass

    @staticmethod
    def flush_terminal_commands(terminal):
        if terminal.workspace_ready and not terminal.workspace_exited and terminal.workspace_pending:
            terminal.workspace_ready = False
            terminal.feed_child(terminal.workspace_pending.pop(0).encode("utf-8"))

    def toggle_shell_output(self):
        if self.mode == "console":
            return_mode = (self.current_workspace_tab.workspace_output_return_mode
                           if self.current_workspace_tab is not None else self.output_return_mode)
            self.set_mode(return_mode if return_mode != "console" else "files")
        else:
            self.set_mode("console")

    def mode_shortcut(self, key):
        key = (key or "").lower()
        if key == "o":
            self.toggle_shell_output()
        elif key == "p":
            self.set_mode("console" if self.mode == "project" else "project")
        elif key == "i":
            self.set_mode("files" if self.mode == "gallery" else "gallery")
        else:
            return False
        return True

    def terminal_shortcut(self, _terminal, event):
        key = (Gdk.keyval_name(event.keyval) or "").lower()
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.ALT_MASK)
        if self.top_tab_shortcut(Gdk.keyval_name(event.keyval), event.state):
            return True
        if (control or shift) and not alt and key in {"return", "kp_enter"}:
            _terminal.feed_child(b"\n")
            return True
        if ((control and shift and key == "v")
                or (shift and not control and key in {"insert", "kp_insert"})) and not alt:
            self.paste_terminal_clipboard(_terminal)
            return True
        if control and shift and not alt and key == "o":
            _terminal.feed_child(b"\x0f")
            return True
        if control and not shift and not alt and key == "o":
            if self.terminal_has_foreground_program(_terminal):
                return False
            self.toggle_shell_output()
            return True
        if control and not shift and not alt and key in {"1", "2", "3", "4"}:
            if self.terminal_has_foreground_program(_terminal):
                return False
            self.set_mode({"1": "files", "2": "project", "3": "console", "4": "gallery"}[key])
            return True
        return False

    def terminal_has_foreground_program(self, terminal):
        record = next((record for record in self.terminals if record[0] is terminal), None)
        if record is None or not record[1]:
            return False
        try:
            pty = terminal.get_pty()
            return pty is not None and os.tcgetpgrp(pty.get_fd()) != os.getpgid(record[1])
        except OSError:
            return False

    def new_terminal(self, command=None, reveal=True):
        terminal = Vte.Terminal()
        terminal.set_scrollback_lines(20000)
        terminal.set_allow_hyperlink(True)
        pcre2_multiline = 0x00000400
        url_regex = Vte.Regex.new_for_match(r"(?:https?|file)://[^[:space:]<>'\"()]+|mailto:[^[:space:]<>'\"()]+", -1,
                                            pcre2_multiline)
        path_regex = Vte.Regex.new_for_match(
            r"(?<![A-Za-z0-9:/])(?:(?:(?:~|\.{1,2})?/|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./+@%~=-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+)(?::[0-9]+(?::[0-9]+)?)?",
            -1, pcre2_multiline)
        terminal.workspace_url_tag = terminal.match_add_regex(url_regex, 0)
        terminal.workspace_path_tag = terminal.match_add_regex(path_regex, 0)
        terminal.match_set_cursor_name(terminal.workspace_url_tag, "pointer")
        terminal.match_set_cursor_name(terminal.workspace_path_tag, "pointer")
        fonts = self.appearance["fonts"]
        terminal_font = Pango.FontDescription()
        terminal_font.set_family(fonts["family"])
        terminal_font.set_absolute_size(fonts["files_px"] * Pango.SCALE)
        terminal_font.set_weight(Pango.Weight(fonts["files_weight"]))
        terminal.set_font(terminal_font)

        def rgba(value):
            color = Gdk.RGBA()
            color.parse(value)
            return color

        terminal.set_colors(rgba(self.palette.get("terminal_foreground", self.palette["foreground"])),
                            rgba(self.palette.get("terminal_background", self.palette["background"])),
                            [rgba(color) for color in self.appearance["ansi"]])
        terminal.set_audible_bell(False)
        terminal.set_scroll_on_keystroke(True)
        connect_click(terminal, self.terminal_menu)
        connect_key(terminal, self.terminal_shortcut, capture=True)
        terminal.workspace_ready = False
        terminal.workspace_spawned = False
        terminal.workspace_ready_source = 0
        terminal.workspace_exited = False
        terminal.workspace_pending = []
        terminal.workspace_mode = "console"
        terminal.workspace_output_return_mode = "files"
        terminal.workspace_browser = (self.capture_browser_state()
                                      if self.current_workspace_tab is None
                                      or self.browser_state_tab is self.current_workspace_tab
                                      else dict(self.current_workspace_tab.workspace_browser))
        if self.mode == "console" and self.current_workspace_tab is not None:
            self.update_shell_cwd(self.current_workspace_tab)
            folder = (self.current_workspace_tab.workspace_cwd
                      or terminal.workspace_browser["paths"][terminal.workspace_browser["active"]])
            paths = list(terminal.workspace_browser["paths"])
            paths[terminal.workspace_browser["active"]] = folder
            terminal.workspace_browser["paths"] = tuple(paths)
        else:
            folder = (self.active.path if self.active is not None
                      else self.initial_browser_state["paths"][0])
        terminal.workspace_cwd = folder
        argv = command or [os.environ.get("SHELL", "/bin/bash"), "-i"]
        self.terminal_sequence += 1
        terminal.workspace_tab_number = self.terminal_sequence
        tab_name = str(folder)
        title = Gtk.Label(label=tab_name)
        page = scrolled(terminal)
        index = self.terminal_tabs.append_page(page, title)
        page.show()
        title.show()
        terminal.workspace_page_index = index
        tab_group = Box(spacing=0)
        tab_button = button(tab_name, lambda target=terminal: self.select_terminal(target.workspace_page_index))
        tab_button.add_css_class("top-tab")
        tab_button.set_tooltip_text(f"{tab_name} · {folder}")
        tab_button.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        tab_button.get_child().set_max_width_chars(24)
        close_button = button("×", lambda target=terminal: self.close_terminal(target))
        close_button.add_css_class("tab-close")
        close_button.set_tooltip_text(f"Close shell in {folder}")
        tab_group.pack_start(tab_button, False, False, 0)
        tab_group.pack_start(close_button, False, False, 0)
        self.terminal_tab_bar.pack_start(tab_group, False, False, 0)
        tab_group.show()
        terminal.workspace_tab_button = tab_button
        terminal.workspace_tab_group = tab_group
        record = [terminal, 0]
        self.terminals.append(record)
        if self.current_workspace_tab is None:
            self.current_workspace_tab = terminal

        def exited(*_):
            record[1] = 0
            terminal.workspace_exited = True
            terminal.workspace_ready = False
            if terminal.workspace_ready_source:
                GLib.source_remove(terminal.workspace_ready_source)
                terminal.workspace_ready_source = 0
            if not getattr(terminal, "workspace_closed", False):
                GLib.idle_add(lambda: self.close_terminal(terminal, force=True, from_shell_exit=True))

        terminal.connect("child-exited", exited)

        def ready():
            terminal.workspace_ready_source = 0
            if not terminal.workspace_spawned or terminal.workspace_exited or terminal.workspace_ready:
                return False
            terminal.workspace_ready = True
            self.flush_terminal_commands(terminal)
            return False

        def output_changed(*_):
            if not terminal.workspace_spawned or terminal.workspace_ready:
                return
            if terminal.workspace_ready_source:
                GLib.source_remove(terminal.workspace_ready_source)
            terminal.workspace_ready_source = GLib.timeout_add(180, ready)

        terminal.connect("contents-changed", output_changed)

        def spawned(_terminal, pid, error, _data):
            record[1] = max(0, pid)
            if error:
                terminal.workspace_exited = True
                self.failure(error)
            else:
                terminal.workspace_spawned = True
                GLib.timeout_add(600, lambda: False if terminal.workspace_ready_source else ready())
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(folder), argv, None,
            GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, spawned, None)
        if reveal:
            self.select_terminal(index)
        else:
            self.refresh_mode_buttons()
        return terminal

    def close_terminal(self, terminal, force=False, from_shell_exit=False):
        index = next((i for i, (candidate, _pid) in enumerate(self.terminals)
                      if candidate is terminal), None)
        if index is None:
            return
        pid = self.terminals[index][1]
        if pid and not force:
            def approved():
                try:
                    os.kill(pid, signal.SIGHUP)
                except ProcessLookupError:
                    pass
                self.close_terminal(terminal, force=True)

            confirm(self, "Close this terminal tab and end its process?", None, approved)
            return
        terminal.workspace_closed = True
        was_active = terminal is self.current_workspace_tab
        if terminal is self.command_terminal:
            self.command_terminal = None
        self.terminal_tabs.remove_page(index)
        self.terminal_tab_bar.remove(terminal.workspace_tab_group)
        self.terminals.pop(index)
        for number, (remaining, _pid) in enumerate(self.terminals):
            remaining.workspace_page_index = number
        if was_active:
            self.current_workspace_tab = None
        if not self.terminals:
            if from_shell_exit:
                self.quit(on_cancel=self.new_terminal)
                return
            self.new_terminal()
        elif was_active:
            self.select_terminal(min(index, len(self.terminals) - 1))
        self.refresh_mode_buttons()

    def terminal_menu(self, terminal, event):
        if event.button == 1 and event.state & Gdk.ModifierType.CONTROL_MASK:
            return self.open_terminal_link(terminal, event)
        if event.button != 3:
            return False
        old_menu = getattr(terminal, "workspace_menu", None)
        if old_menu is not None:
            old_menu.popdown()
            old_menu.unparent()
        menu = Gtk.Popover()
        menu.set_parent(terminal)
        point = Gdk.Rectangle()
        point.x, point.y = int(event.x), int(event.y)
        point.width = point.height = 1
        menu.set_pointing_to(point)
        content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=6)
        for title, callback in (("Copy", lambda: terminal.copy_clipboard_format(Vte.Format.TEXT)),
                                ("Paste", lambda: self.paste_terminal_clipboard(terminal)),
                                ("Send Ctrl+O", lambda: terminal.feed_child(b"\x0f"))):
            content.append(button(title, lambda action=callback: (action(), menu.popdown())))
        menu.set_child(content)
        terminal.workspace_menu = menu
        menu.popup()
        return True

    def paste_terminal_clipboard(self, terminal):
        if terminal.workspace_exited or getattr(terminal, "workspace_closed", False):
            return
        formats = Gdk.Display.get_default().get_clipboard().get_formats()
        has_image = (formats.contain_gtype(Gdk.Texture.__gtype__)
                     or any(mime.startswith("image/") for mime in formats.get_mime_types() or ()))
        if has_image and self.terminal_has_foreground_program(terminal):
            # The foreground TUI (for example Codex) reads the image itself.
            terminal.feed_child(b"\x16")
        else:
            terminal.paste_clipboard()

    def open_terminal_link(self, terminal, event):
        uri = terminal.check_hyperlink_at(event.x, event.y)
        if not uri:
            match, tag = terminal.check_match_at(event.x, event.y)
            if not match:
                return False
            if tag == terminal.workspace_url_tag:
                uri = match.rstrip('.,;:!?)\"]')
            elif tag == terminal.workspace_path_tag:
                path_text = re.sub(r":\d+(?::\d+)?$", "", match)
                path = Path(path_text).expanduser()
                if not path.is_absolute():
                    path = Path(terminal.workspace_cwd) / path
                if not path.exists():
                    return False
                uri = path.absolute().as_uri()
            else:
                return False
        if urlsplit(uri).scheme not in {"http", "https", "file", "mailto"}:
            return False
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as error:
            self.failure(error)
        return True

    def build_agents(self):
        box = Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=15)
        controls = Box(spacing=8)
        self.profile_picker = Gtk.ComboBoxText()
        self.session_picker = Gtk.ComboBoxText()
        controls.pack_start(self.profile_picker, False, False, 0)
        controls.pack_start(self.session_picker, True, True, 0)
        controls.pack_start(button("Refresh", self.refresh_agents), False, False, 0)
        controls.pack_start(button("New", self.new_agent_session), False, False, 0)
        self.profile_picker.connect("changed", lambda *_: self.profile_changed())
        self.session_picker.connect("changed", lambda *_: self.load_agent_session())
        box.pack_start(controls, False, False, 0)
        self.agent_info = label("Connect to the existing local agent service. Folder contents are shared only when you send them.", "muted")
        box.pack_start(self.agent_info, False, False, 0)
        self.chat = textview()
        box.pack_start(scrolled(self.chat), True, True, 0)
        self.compose = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.compose.set_left_margin(12)
        self.compose.set_top_margin(10)
        composer = scrolled(self.compose)
        composer.set_size_request(-1, 100)
        box.pack_start(composer, False, False, 0)
        self.compose.get_buffer().connect("changed", self.draft_changed)
        self.send_button = button("Send to selected agent", self.send_agent)
        self.send_button.set_sensitive(False)
        box.pack_start(self.send_button, False, False, 0)
        return box

    def draft_changed(self, *_):
        profile = self.profile_picker.get_active_id()
        if profile:
            buffer = self.compose.get_buffer()
            self.agent_drafts[profile] = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            self.agent_request_key = None

    def refresh_agents(self):
        if self.agent_loading:
            return
        self.agent_loading = True
        self.agent_info.set_text("Connecting…")

        def ready(snapshot):
            self.agent_loading = False
            selected = self.profile_picker.get_active_id() or (self.context or {}).get("agent")
            self.agent_snapshot = snapshot
            self.profile_picker.remove_all()
            for profile in snapshot.get("profiles", []):
                self.profile_picker.append(profile["id"], profile["name"])
            if selected:
                self.profile_picker.set_active_id(selected)
            if self.profile_picker.get_active() < 0:
                self.profile_picker.set_active(0)
            self.profile_changed()

        def failed(exc):
            self.agent_loading = False
            self.agent_info.set_text("Agent service unavailable. Files and terminals remain available.")
            self.send_button.set_sensitive(False)
        self.async_call(self.agent_client.snapshot, ready, failed)

    def profile_changed(self):
        identifier = self.profile_picker.get_active_id()
        if not identifier:
            return
        self.agent_session = None
        self.session_picker.remove_all()
        for session in self.agent_snapshot.get("sessions", []):
            if session["profile"] == identifier:
                self.session_picker.append(session["id"], session["title"])
        self.session_picker.set_active(0)
        profile = next(p for p in self.agent_snapshot["profiles"] if p["id"] == identifier)
        self.agent_info.set_text(f"{profile['name']} · {profile.get('session_mode', '')} · {profile.get('execution', '')} · " + (profile.get("collection") or "No Library access"))
        draft = self.agent_drafts.get(identifier, "")
        self.compose.get_buffer().set_text(draft)
        self.send_button.set_sensitive(not self.agent_sending)

    def new_agent_session(self):
        identifier = self.profile_picker.get_active_id()
        if not identifier or self.agent_sending:
            return
        self.agent_session = None
        self.pending_agent_sends.pop(identifier, None)
        self.session_picker.set_active(-1)
        self.chat.get_buffer().set_text("A new conversation starts when you send your first message.")

    def load_agent_session(self):
        profile, session = self.profile_picker.get_active_id(), self.session_picker.get_active_id()
        self.agent_session = session
        if not profile or not session:
            self.chat.get_buffer().set_text("Choose a conversation, or send a message to start one.")
            return

        def ready(result):
            if profile != self.profile_picker.get_active_id() or session != self.agent_session:
                return
            transcript = "\n\n".join(m["role"].upper() + "\n" + m["content"] for m in result.get("messages", []))
            self.chat.get_buffer().set_text(transcript or "No messages yet.")
            if result.get("status") == "running":
                GLib.timeout_add(2000, lambda: (self.load_agent_session(), False)[1] if session == self.agent_session else False)
        self.async_call(lambda: self.agent_client.detail(profile, session), ready)

    def send_agent(self):
        profile = self.profile_picker.get_active_id()
        buffer = self.compose.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not profile or not text or self.agent_sending:
            return
        mode = next(p.get("session_mode") for p in self.agent_snapshot["profiles"] if p["id"] == profile)
        session = None if mode == "one_off" else self.agent_session
        pending = self.pending_agent_sends.get(profile)
        if not pending or pending["text"] != text:
            pending = {"profile": profile, "key": uuid.uuid4().hex, "text": text, "session": session}
            self.pending_agent_sends[profile] = pending
        request_key = pending["key"]
        self.agent_sending = True
        self.send_button.set_sensitive(False)
        self.profile_picker.set_sensitive(False)
        self.session_picker.set_sensitive(False)

        def submit():
            if pending["session"] is None:
                pending["session"] = self.agent_client.create(profile)["id"]
            target = pending["session"]
            return self.agent_client.send(profile, target, request_key, text)

        def ready(result):
            self.agent_sending = False
            self.agent_session = result["id"]
            current = self.compose.get_buffer()
            current_text = current.get_text(current.get_start_iter(), current.get_end_iter(), False).strip()
            if current_text == text:
                current.set_text("")
            self.agent_request_key = None
            self.pending_agent_sends.pop(profile, None)
            self.send_button.set_sensitive(True)
            self.profile_picker.set_sensitive(True)
            self.session_picker.set_sensitive(True)
            self.load_agent_session_direct(profile, result["id"])

        def failed(exc):
            self.agent_sending = False
            self.send_button.set_sensitive(True)
            self.profile_picker.set_sensitive(True)
            self.session_picker.set_sensitive(True)
            self.agent_info.set_text(str(exc) + ". Your draft is retained.")
        self.async_call(submit, ready, failed)

    def load_agent_session_direct(self, profile, session):
        if profile != self.profile_picker.get_active_id():
            return
        self.session_picker.append(session, "Current conversation")
        self.session_picker.set_active_id(session)

    def hide_window(self, *_):
        self.hide()
        return True

    def quit(self, confirmed=False, on_cancel=None):
        active_terminals = any(pid for _terminal, pid in self.terminals)
        active_jobs = any(j.status in {"queued", "copying"} for j in self.jobs)
        if (active_terminals or active_jobs) and not confirmed:
            question = ("Quit Nightty and close its terminals?" if active_terminals
                        else "Quit Nightty and cancel unfinished copies?")
            confirm(self, question,
                    "Hiding keeps everything running. Quitting closes embedded terminals and cancels unfinished copies; completed files are kept.",
                    lambda: self.quit(confirmed=True), on_cancel)
            return False
        for job in self.jobs:
            job.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.listing_pool.shutdown(wait=False, cancel_futures=True)
        self.thumbnails_pool.shutdown(wait=False, cancel_futures=True)
        self.transfers_pool.shutdown(wait=False, cancel_futures=True)
        self.app.quit()
        return True

    def key(self, _widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.ALT_MASK)
        key = Gdk.keyval_name(event.keyval)
        terminal_focus = any(terminal.has_focus() for terminal, _pid in self.terminals)
        if self.top_tab_shortcut(key, event.state):
            return True
        if terminal_focus and not (
                ctrl and (not shift and key.lower() in {"o", "q", "1", "2", "3", "4"}
                          or shift and key.lower() in {"o", "t", "c", "v"})
                or key == "F12"):
            return False
        if terminal_focus and ctrl and shift and key.lower() == "o":
            self.current_workspace_tab.feed_child(b"\x0f")
            return True
        if (terminal_focus and ctrl and not shift and key.lower() in {"o", "1", "2", "3", "4"}
                and self.terminal_has_foreground_program(self.current_workspace_tab)):
            return False
        if ctrl and key in {"1", "2", "3", "4"}:
            self.set_mode({"1": "files", "2": "project", "3": "console", "4": "gallery"}[key])
        elif ctrl and not shift and not alt and self.mode_shortcut(key):
            pass
        elif ctrl and key == "5":
            self.toggle_quick_view()
        elif alt and key in {"1", "2", "F1", "F2"}:
            if self.mode != "files":
                self.set_mode("files")
            self.show_locations(self.panes[0 if key in {"1", "F1"} else 1])
        elif alt and key.lower() == "s":
            self.active.show_search()
        elif ctrl and shift and key.lower() == "t":
            self.new_terminal()
        elif terminal_focus and ctrl and shift and key.lower() in {"c", "v"}:
            index = self.terminal_tabs.get_current_page()
            terminal = self.terminals[index][0]
            if key.lower() == "c":
                terminal.copy_clipboard_format(Vte.Format.TEXT)
            else:
                self.paste_terminal_clipboard(terminal)
        elif ctrl and key.lower() == "q":
            self.quit()
        elif key == "F12":
            self.hide()
        elif key == "Escape" and self.mode == "inspect" and not terminal_focus:
            self.close_inspector()
        elif key == "Escape" and self.mode == "files" and self.active.search_row.get_visible():
            self.active.hide_search()
        elif terminal_focus:
            return False
        elif ctrl and key.lower() == "l":
            self.set_mode("files")
            self.active.location.grab_focus()
            self.active.location.select_region(0, -1)
        elif ctrl and key.lower() == "d":
            self.add_favourite()
        elif ctrl and key.lower() == "h":
            self.active.hidden = not self.active.hidden
            self.active.navigate(self.active.path)
        elif alt and key == "Up":
            self.active.up()
        elif (key in {"Tab", "ISO_Left_Tab"} and self.mode == "files" and not terminal_focus
              and not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                                     | Gdk.ModifierType.SUPER_MASK)):
            if self.quick_enabled:
                self.toggle_quick_view()
            self.switch_browser_pane()
        elif (key in {"Tab", "ISO_Left_Tab"} and self.mode == "gallery"
              and not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                                     | Gdk.ModifierType.SUPER_MASK)):
            self.switch_browser_pane()
        elif key == "F3":
            self.close_inspector() if self.mode == "inspect" else self.preview()
        elif key == "F4":
            self.open_external()
        elif key == "F5" and self.mode in {"files", "gallery"}:
            self.copy()
        else:
            return False
        return True
