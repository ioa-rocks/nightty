# SPDX-License-Identifier: MPL-2.0
"""One directory pane: MC-style two-column file list, search results, sort and location controls."""

import argparse
import math
from pathlib import Path
import queue
import threading

from gi.repository import Gdk, GLib, Gtk, Pango

from .gallery import Gallery
from .model import directory_entries, sort_directory_rows
from .widgets import (Box, button, connect_click, connect_focus, connect_key, drawing_overlay, label,
                      on_resize, scrolled)


class Pane(Box):
    def __init__(self, window, folder, index):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window, self.path, self.index = window, Path(folder), index
        self.previous_path = None
        self.hidden, self.generation = False, 0
        self.gallery = Gallery(self)
        self.sort_key = "name"
        self.sort_reverse = False
        self.created_loaded = False
        toolbar = Box(spacing=2, margin=7)
        toolbar.add_css_class("pane-toolbar")
        toolbar.set_margin_end(2)
        self.sort_button = button("n", lambda: self.window.show_pane_menu(self, "sort", self.sort_button))
        self.sort_button.set_has_frame(False)
        self.sort_button.add_css_class("pane-header-control")
        self.sort_button.set_tooltip_text("Sort this pane: name, ascending")
        toolbar.pack_start(self.sort_button, False, False, 0)
        self.view_button = Gtk.Button.new_from_icon_name("view-list-symbolic")
        self.view_button.set_has_frame(False)
        self.view_button.connect("clicked", lambda *_: self.window.show_pane_menu(self, "view", self.view_button))
        self.view_button.add_css_class("pane-header-control")
        self.view_button.set_tooltip_text("View: Files; choose Files, Gallery, or Info")
        toolbar.pack_start(self.view_button, False, False, 0)
        self.location_bar = Gtk.Overlay()
        self.location = Gtk.Entry()
        self.location.add_css_class("pane-address-entry")
        self.location.set_text(str(self.path))
        self.location.set_width_chars(4)
        self.location.set_tooltip_text(str(self.path))
        self.location.connect("activate", lambda entry: self.navigate(Path(entry.get_text()).expanduser()))
        self.location_bar.set_child(self.location)
        self.locations_button = Gtk.Button.new_from_icon_name("non-starred-symbolic")
        self.locations_button.set_has_frame(False)
        self.locations_button.connect("clicked", lambda *_: self.window.show_locations(self))
        self.locations_button.add_css_class("pane-header-control")
        self.locations_button.set_tooltip_text("Locations and favourites (Ctrl+F1 / Ctrl+F2)")
        toolbar.pack_start(self.locations_button, False, False, 0)
        self.find_button = Gtk.Button()
        self.find_button.set_child(Gtk.Image.new_from_icon_name("edit-find-symbolic"))
        self.find_button.set_has_frame(False)
        self.find_button.add_css_class("pane-header-control")
        self.find_button.add_css_class("pane-address-control")
        self.find_button.set_tooltip_text("Find files in this tree or the plocate index (Alt+F7)")
        self.find_button.update_property([Gtk.AccessibleProperty.LABEL], ["Find files"])
        self.find_button.connect("clicked", lambda *_: self.window.show_find_dialog(self))
        self.find_button.set_halign(Gtk.Align.END)
        self.find_button.set_valign(Gtk.Align.CENTER)
        self.location_bar.add_overlay(self.find_button)
        toolbar.pack_start(self.location_bar, True, True, 0)
        self.pack_start(toolbar, False, False, 0)
        self.search_row = Box(spacing=6, margin=7)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Go to filename in this folder")
        self.search.connect("changed", self.search_changed)
        self.search.connect("activate", lambda *_: self.hide_search())
        connect_key(self.search, self.search_key)
        self.search_row.pack_start(self.search, True, True, 0)
        self.search_result = label("", "muted")
        self.search_row.pack_start(self.search_result, False, False, 0)
        self.search_row.pack_start(button("×", self.hide_search), False, False, 0)
        self.pack_start(self.search_row, False, False, 0)
        self.results_active = False
        self.results_serial = 0
        self.results_cancel = None
        self.results_query = ""
        self.results_scope = "tree"
        self.results_matches = []
        self.results_created_loaded = False
        self.results_saved_marks = set()
        self.results_restore_name = None
        self.results_row = Box(spacing=8, margin=7)
        self.results_row.add_css_class("search-results-bar")
        self.results_label = label("", "search-results-label")
        self.results_row.pack_start(self.results_label, True, True, 0)
        self.results_row.pack_end(button("Close  Esc", self.close_results), False, False, 0)
        self.results_row.pack_end(button("Refresh", lambda: self.window.repeat_find(self, reveal=True)), False, False, 0)
        self.pack_start(self.results_row, False, False, 0)
        self.results_row.hide()
        self.store = Gtk.ListStore(str, str, str, str, str)
        self.view = Gtk.IconView(model=self.store)
        self.view.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.view.set_columns(2)
        self.view.set_item_width(300)
        # Item padding adds a background-colored rim and enlarges every row.
        self.view.set_item_padding(0)
        self.view.set_row_spacing(0)
        self.file_column_spacing = 2
        self.view.set_column_spacing(self.file_column_spacing)
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
        self.prefix_xpad = 2
        marker = Gtk.CellRendererText(xpad=self.prefix_xpad, ypad=0, width_chars=1,
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
        connect_key(self.view, self.move_by_key, capture=True)
        connect_click(self.view, self.focus_nearest_on_blank, capture=True)
        connect_focus(self.view, lambda *_: self.window.activate_pane(self))
        self.view.connect("item-activated", self.open)
        self.cursor_handler = self.view.connect("selection-changed", self.selected)
        self.scroll = scrolled(self.view)
        on_resize(self.scroll, self.resize_columns)
        self.scroll.get_vadjustment().connect("value-changed", self.scroll_changed)
        self.scroll_overlay = drawing_overlay(self.scroll, self.draw_column_divider)
        self.content_stack = Gtk.Stack()
        self.content_stack.set_vhomogeneous(False)
        self.content_stack.add_css_class("pane-content-left" if self.index == 0 else "pane-content-right")
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
        self.status_box.add_css_class("pane-footer-left" if self.index == 0 else "pane-footer-right")
        self.status = label("", "footer-details")
        self.status.set_selectable(False)
        # Keep the footer steady when a fallback font has taller CJK glyphs.
        self.status.set_size_request(-1, math.ceil(self.window.appearance["fonts"]["small_px"] * 1.5))
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
        if self.results_active:
            self.leave_results()
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
        requested_created = self.sort_key == "created"
        self.created_loaded = False
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
        self.location.set_tooltip_text(str(path))
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
                self.show_directory_rows(self.scan_rows, loading=True)
            return True

        self.scan_flush_source = GLib.timeout_add(80, flush)

        def loaded(rows):
            if generation != self.generation:
                return
            self.created_loaded = requested_created
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
                self.gallery.show()
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
            on_batch=batches.put, cancelled=lambda: generation != self.generation,
            include_created=requested_created), loaded, failed,
            executor=self.window.listing_pool)

    def show_directory_rows(self, rows, loading=False):
        path = self.path
        keep_focus = (self is self.window.active and self.window.mode == "files"
                      and self.window.get_focus() is self.view)
        rows = sort_directory_rows(rows, self.sort_key, self.sort_reverse)
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
        for name, directory, symlink, size, executable, _modified, _created in rows:
            prefix = "@" if symlink else "/" if directory else "*" if executable else ""
            entry_path = str(path / name)
            entries.append([prefix + name, name,
                "" if directory else self.window.size(size), entry_path,
                self.window.file_color(name, directory, symlink, executable)])
            if directory:
                directories.add(entry_path)
        self.entries = entries
        self.entry_sizes = {str(path / name): size for name, directory, _symlink, size,
                            _exec, _modified, _created in rows
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
        if self.results_active:
            self.close_results()
        else:
            self.navigate(self.path.parent)

    def refresh(self):
        if self.results_active:
            self.window.repeat_find(self)
            return
        current = self.cursor_logical()
        name = self.entries[current][1] if current is not None else None
        self.navigate(self.path, selected_name=name)

    def begin_results(self, query, scope, preserve_marks=False, focus=True):
        if not self.results_active:
            current = self.cursor_logical()
            self.results_restore_name = (self.entries[current][1]
                                         if current is not None and current < len(self.entries) else None)
            self.results_saved_marks = self.marked_paths.copy()
        elif self.results_cancel is not None:
            self.results_cancel.set()
        self.results_serial += 1
        self.results_cancel = threading.Event()
        self.results_active = True
        self.results_query, self.results_scope = query, scope
        self.results_matches = []
        self.results_created_loaded = False
        if not preserve_marks:
            self.marked_paths.clear()
        self.generation += 1  # Discard any unfinished directory listing.
        self.stop_model_fill()
        if self.scan_flush_source:
            GLib.source_remove(self.scan_flush_source)
            self.scan_flush_source = 0
        if self.placeholder_source:
            GLib.source_remove(self.placeholder_source)
            self.placeholder_source = 0
        if self.search_row.get_visible():
            self.search_row.hide()
            self.search.set_text("")
        self.pending_restore_name = None
        self.results_row.show()
        self.show_result_rows(loading=True)
        if focus:
            self.view.grab_focus()
        return self.results_serial, self.results_cancel

    def leave_results(self):
        if self.results_cancel is not None:
            self.results_cancel.set()
        self.results_serial += 1
        self.results_active = False
        self.results_row.hide()
        self.results_matches = []
        self.marked_paths = self.results_saved_marks

    def close_results(self):
        if not self.results_active:
            return
        restore_name = self.results_restore_name
        self.leave_results()
        self.navigate(self.path, selected_name=restore_name)
        self.view.grab_focus()

    def show_result_rows(self, loading=False, outcome=None, error=None):
        old_entries = self.entries
        current = self.current_path() if old_entries else None
        entries = []
        directories = set()
        sizes = {}
        matches = sorted(self.results_matches,
                         key=lambda match: (match.path.name.casefold(), str(match.path.parent).casefold()))
        key = self.sort_key
        if key == "extension":
            matches.sort(key=lambda match: match.path.suffix.casefold(), reverse=self.sort_reverse)
        elif key in {"size", "modified", "created"}:
            matches.sort(key=lambda match: getattr(match, key) or 0, reverse=self.sort_reverse)
            if key == "created":
                matches.sort(key=lambda match: match.created is None)
        elif self.sort_reverse:
            matches.reverse()
        matches.sort(key=lambda match: not match.directory)
        for match in matches:
            path = str(match.path)
            name = match.path.name
            prefix = "@" if match.symlink else "/" if match.directory else "*" if match.executable else ""
            entries.append([prefix + name, name,
                            "" if match.directory else self.window.size(match.size), path,
                            self.window.file_color(name, match.directory, match.symlink, match.executable)])
            if match.directory:
                directories.add(path)
            else:
                sizes[path] = match.size
        self.entries = entries
        self.directory_paths = directories
        self.entry_sizes = sizes
        if not loading:
            self.marked_paths.intersection_update(row[3] for row in entries)
        count = len(entries)
        if error:
            summary = f"Search failed: {error}"
        elif loading:
            summary = f"{count} found · searching…"
        else:
            summary = f"{count} found" + (" · limit reached" if outcome and outcome.truncated else "")
            if outcome and outcome.skipped:
                summary += f" · {outcome.skipped} inaccessible"
        self.count.set_text(summary)
        scope_label = "this tree" if self.results_scope == "tree" else "indexed files"
        self.results_label.set_text(f"FIND  {self.results_query}  ·  {scope_label}  ·  {summary}")
        self.rebuild_entries(preserve=bool(old_entries and current is not None and
                                           str(current) in {row[3] for row in entries}))
        if self is self.window.active and self.window.mode in {"files", "gallery"}:
            selected = self.selected_paths()
            self.window.selection = selected[0] if selected else None
        self.window.refresh_file_footer(self)

    def favourite(self):
        self.window.state.toggle_favourite(self.path)
        self.window.refresh_star_buttons()

    def set_sort(self, criterion):
        if criterion == self.sort_key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key, self.sort_reverse = criterion, False
        self.window.refresh_pane_controls(self)
        if self.results_active:
            if criterion == "created" and not self.results_created_loaded:
                self.window.repeat_find(self, reveal=False)
            else:
                self.show_result_rows()
        elif criterion == "created" and not self.created_loaded:
            self.refresh()
        else:
            self.show_directory_rows(self.listed_rows)
            if self.content_stack.get_visible_child_name() == "gallery":
                self.gallery.show()

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

    def select_logical(self, logical, top=None):
        position = self.store_position(logical, top)
        if position is not None:
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
                self.window.activate_pane(self, update=self.window.active is not self)
                self.view.grab_focus()
                return True
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
        position = self.store_position(target)
        if position is None and self.store_to_logical:
            position = self.store_position(max(self.store_to_logical.values()))
        if position is None:
            return True
        cursor = Gtk.TreePath.new_from_indices([position])
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
                self.select_logical(logical, next_top)
        elif not control:
            self.select_logical(target, next_top)
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
        if hasattr(self, "scroll_overlay"):
            self.scroll_overlay.drawing_area.queue_draw()
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
            self.select_logical(target, top)
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
        self.scroll_overlay.drawing_area.queue_draw()
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
                        if not any(self.view.path_is_selected(Gtk.TreePath.new_from_indices([existing]))
                                   for existing in path_to_stores[entry[3]][:-1]):
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
            self.scroll_overlay.drawing_area.queue_draw()

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
        self.scroll_overlay.drawing_area.queue_draw()
        if target is not None:
            path = Gtk.TreePath.new_from_indices([self.store_position(target)])
            self.view.set_cursor(path, None, False)
            self.selection_anchor = target
        for entry_path in selected:
            positions = self.path_to_stores.get(entry_path, ())
            logical = self.store_to_logical.get(positions[0]) if positions else None
            position = self.store_position(logical) if logical is not None else None
            if position is not None:
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
        if self.results_active and model[row][3]:
            parent = Path(model[row][3]).parent
            if self.results_scope == "tree":
                try:
                    parent = parent.relative_to(self.path)
                except ValueError:
                    pass
            renderer.set_property("text", f"{model[row][1]}  ·  {parent}")
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

    def column_visible_bottom(self, height):
        # IconView's visible range can lag a model swap after a fast navigation.
        pitch = max(1, self.item_pitch())
        scroll = self.scroll.get_vadjustment().get_value()
        model_bottom = self.top_inset + self.model_next_start * pitch - scroll
        full_rows = max(0, math.floor((height + scroll - self.top_inset) / pitch))
        viewport_bottom = self.top_inset + full_rows * pitch - scroll
        return max(0, min(height, model_bottom, viewport_bottom))

    def draw_column_divider(self, widget, context):
        height = widget.get_allocated_height()
        visible_bottom = self.column_visible_bottom(height)
        background = Gdk.RGBA()
        background.parse(self.window.palette["background"])
        context.set_source_rgba(background.red, background.green, background.blue, background.alpha)
        context.rectangle(0, visible_bottom, self.view.get_allocated_width(), height - visible_bottom)
        context.fill()
        color = Gdk.RGBA()
        color.parse(self.window.palette["border"])
        context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        # IconView places column two before the viewport midpoint when it
        # reserves room for a scrollbar. Match the outer border's marker gap.
        x = self.view.get_item_width() + self.file_column_spacing + self.prefix_xpad
        context.rectangle(x, 0, 2, height)
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
        cursor = self.view.get_cursor()[1]
        invalid = [path for path in self.view.get_selected_items()
                   if path.get_indices()[0] not in self.store_to_logical]
        invalid_cursor = cursor is not None and cursor.get_indices()[0] not in self.store_to_logical
        if self.entries and (invalid or invalid_cursor):
            source = invalid[0] if invalid else cursor
            position = self.store_position(self.nearest_logical(source.get_indices()[0]))
            if position is not None:
                path = Gtk.TreePath.new_from_indices([position])
                self.view.handler_block(self.cursor_handler)
                for empty in invalid:
                    self.view.unselect_path(empty)
                self.view.set_cursor(path, None, False)
                self.view.select_path(path)
                self.view.handler_unblock(self.cursor_handler)
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
            cursor_path = self.view.get_cursor()[1]
            cursor_row = cursor_path.get_indices()[0] if cursor_path is not None else None
            self.view.handler_block(self.cursor_handler)
            for logical in visible | selected_logicals:
                positions = self.logical_to_stores.get(logical, ())
                preferred = (cursor_row if cursor_row in positions else
                             self.store_position(logical, top)) if logical in keep else None
                for position in positions:
                    path = Gtk.TreePath.new_from_indices([position])
                    if position == preferred:
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
