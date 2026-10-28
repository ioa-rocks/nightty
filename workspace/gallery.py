# SPDX-License-Identifier: MPL-2.0
"""Gallery view of one directory pane: horizontal cards in sliding columns."""

from datetime import datetime
import math
import textwrap

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from .previews import format_size, pdf_pixbuf
from .widgets import (Box, connect_click, connect_focus, connect_key, drawing_overlay, on_resize,
                      scrolled, widget_children)


def geometry(flow, children):
    """Columns, row pitch and first-row y of a laid-out flow box."""
    first = children[0]
    first_y = first.get_allocation().y
    columns = 2 if len(children) > 1 and children[1].get_allocation().y == first_y else 1
    pitch = (children[columns].get_allocation().y - first_y if len(children) > columns
             else 0)
    if pitch <= 0:
        pitch = first.get_allocated_height() + flow.get_row_spacing()
    return columns, pitch, first_y


class Gallery:
    """Cards for one pane, built lazily; the window owns mode and focus, the pane owns the folder.

    Cards fill the left column top to bottom, then the right column; ``children_by_logical``
    maps that logical order to flow children and ``replicas_by_logical`` lists duplicates that
    keep both columns full on the last screen.
    """

    def __init__(self, pane):
        self.pane, self.window = pane, pane.window
        self.flow = self.scroll = self.overlay = None
        self.serial = 0
        self.capacity, self.columns = 1, 2
        self.resize_source = 0
        self.building = False
        self.scroll_snap_source = 0
        self.children_by_logical = []
        self.replicas_by_logical = {}
        self.selection_syncing = False
        self.displayed_path = None
        self.build_waiting = False
        self.preview_results = {}
        self.render_waiters = {}
        self.pending_target = None
        self.pending_top = None

    def build(self):
        """Create the flow box, its scroller and the edge overlay inside the pane's stack."""
        pane, window = self.pane, self.window
        self.flow = Gtk.FlowBox()
        self.flow.set_can_focus(True)
        self.flow.set_focusable(True)
        self.flow.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        self.flow.set_activate_on_single_click(False)
        self.flow.set_row_spacing(8)
        self.flow.set_column_spacing(8)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(2)
        self.flow.set_homogeneous(True)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_margin_top(7)
        self.flow.set_margin_start(7)
        self.flow.set_margin_end(7)
        self.flow.set_margin_bottom(120)
        self.flow.connect("selected-children-changed", self.selected)
        self.flow.connect("child-activated", self.activated)
        connect_key(self.flow, self.key)
        connect_focus(self.flow, lambda _flow, _event: window.activate_pane(pane))
        connect_click(self.flow, lambda flow, event: self.click(flow, event))
        self.scroll = scrolled(self.flow)
        on_resize(self.scroll, lambda _scroll, _allocation: self.resized())
        self.scroll.get_vadjustment().connect("value-changed", lambda _adjustment: self.scroll_changed())
        self.overlay = drawing_overlay(self.scroll, lambda scroll, context: self.draw_edge(scroll, context))
        connect_click(self.scroll, lambda scroll, event: self.click(scroll, event))
        pane.content_stack.add_named(self.overlay, "gallery")
        self.scroll.show()

    def click(self, widget, event, child=None):
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.window.activate_pane(self.pane)
        flow = self.flow
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        top = self.top()
        visible = [item for item in widget_children(flow)
                   if (hasattr(item, "gallery_index")
                       and top <= item.gallery_row < top + self.capacity)]
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
        self.scroll_to(child)
        return True

    def selected(self, flow):
        if self.window.mode != "gallery" or flow is not self.window.gallery_flow:
            return
        if self.selection_syncing:
            return
        selected_children = [child for child in flow.get_selected_children()
                             if hasattr(child, "gallery_pane")]
        selected_indices = {child.gallery_index for child in selected_children}
        self.selection_syncing = True
        try:
            for logical, replicas in self.replicas_by_logical.items():
                for child in replicas:
                    if logical in selected_indices and not child.is_selected():
                        flow.select_child(child)
                    elif logical not in selected_indices and child.is_selected():
                        flow.unselect_child(child)
        finally:
            self.selection_syncing = False
        paths = self.selected_paths()
        selected = {str(child.file_path) for child in selected_children}
        self.pane.view.handler_block(self.pane.cursor_handler)
        self.pane.view.unselect_all()
        for path in selected:
            for position in self.pane.path_to_stores.get(path, ()):
                self.pane.view.select_path(Gtk.TreePath.new_from_indices([position]))
        self.pane.view.handler_unblock(self.pane.cursor_handler)
        self.window.selection = paths[0] if paths else None
        self.window.refresh_file_footer(self.pane)

    def scroll_to(self, child):
        flow = self.flow
        if child.get_parent() is not flow:
            return False
        children = widget_children(flow)
        if not children or not hasattr(child, "gallery_row"):
            return False
        _columns, pitch, _first_y = geometry(flow, children)
        top = self.top()
        if child.gallery_row < top:
            top = child.gallery_row
        elif child.gallery_row >= top + self.capacity:
            top = child.gallery_row - self.capacity + 1
        self.scroll.get_vadjustment().set_value(min(self.max_top(), top) * pitch)
        return False

    def max_top(self):
        return max(0, len(self.children_by_logical) -
                   self.columns * self.capacity)

    def top(self):
        children = widget_children(self.flow)
        if len(children) < 2:
            return 0
        _columns, pitch, _first_y = geometry(self.flow, children)
        return min(self.max_top(), max(0, round(
            self.scroll.get_vadjustment().get_value() / pitch)))

    def child_for_index(self, logical, top):
        replicas = self.replicas_by_logical.get(logical, ())
        return next((child for child in replicas
                     if top <= child.gallery_row < top + self.capacity),
                    min(replicas, key=lambda child: abs(child.gallery_row - top)) if replicas else None)

    def visible_bottom(self):
        flow = self.flow
        children = widget_children(flow)
        if not children or children[0].get_allocated_height() <= 1:
            return self.scroll.get_allocated_height()
        _columns, pitch, _first_y = geometry(flow, children)
        origin = children[0].translate_coordinates(self.scroll, 0, 0)
        if origin is None:
            return self.scroll.get_allocated_height()
        first_top = origin[1]
        viewport = self.scroll.get_allocated_height()
        card_height = children[0].get_allocated_height()
        last_full_row = math.floor((viewport - first_top - card_height) / pitch)
        if last_full_row < 0:
            return 0
        return max(0, min(viewport, first_top + last_full_row * pitch + card_height))

    def draw_edge(self, scroll, context):
        height = scroll.get_allocated_height()
        bottom = self.visible_bottom()
        if bottom < height:
            background = Gdk.RGBA()
            background.parse(self.window.palette["background"])
            context.set_source_rgba(background.red, background.green, background.blue, background.alpha)
            context.rectangle(0, bottom, self.flow.get_allocated_width(), height - bottom)
            context.fill()
        border = Gdk.RGBA()
        border.parse(self.window.palette["border"])
        context.set_source_rgba(border.red, border.green, border.blue, border.alpha)
        context.rectangle(0, 0, 2, height)
        context.fill()
        return False

    def scroll_changed(self):
        if self.scroll_snap_source:
            GLib.source_remove(self.scroll_snap_source)
        self.scroll_snap_source = GLib.timeout_add(45, self.snap_scroll)
        self.overlay.drawing_area.queue_draw()

    def snap_scroll(self):
        self.scroll_snap_source = 0
        children = widget_children(self.flow)
        if len(children) > 1:
            _columns, pitch, _first_y = geometry(self.flow, children)
            adjustment = self.scroll.get_vadjustment()
            maximum = max(0, adjustment.get_upper() - adjustment.get_page_size())
            target = min(round(adjustment.get_value() / pitch), self.max_top(),
                         math.floor(maximum / pitch)) * pitch
            if abs(adjustment.get_value() - target) > 0.5:
                adjustment.set_value(target)
        return False

    def layout(self):
        flow = self.flow
        children = widget_children(flow)
        width = self.scroll.get_allocated_width()
        columns = 2 if width >= 2 * 310 + flow.get_column_spacing() + 14 else 1
        card_height, pitch = 108, 108 + flow.get_row_spacing()
        first_y = flow.get_margin_top()
        if len(children) > 1 and children[0].get_allocated_height() > 1:
            _old_columns, measured_pitch, _first_y = geometry(flow, children)
            card_height = max(108, children[0].get_allocated_height())
            pitch = max(card_height + flow.get_row_spacing(), measured_pitch)
        viewport = self.scroll.get_allocated_height()
        capacity = max(1, math.floor((viewport - first_y - card_height) / pitch) + 1)
        return columns, capacity

    def resized(self):
        if self.pane.content_stack.get_visible_child_name() != "gallery":
            return
        if self.build_waiting and self.scroll.get_allocated_width() > 1:
            self.show()
            return
        if self.resize_source:
            GLib.source_remove(self.resize_source)
        self.resize_source = GLib.timeout_add(120, self.apply_resize)

    def apply_resize(self):
        self.resize_source = 0
        if (not self.building
                and self.pane.content_stack.get_visible_child_name() == "gallery"
                and widget_children(self.flow)
                and self.layout() != (self.columns, self.capacity)):
            self.show()
        return False

    def selected_paths(self):
        flow = self.flow
        paths = []
        seen = set()
        for child in sorted(flow.get_selected_children(),
                            key=lambda item: getattr(item, "gallery_index", -1)):
            if (hasattr(child, "gallery_index") and not child.is_parent
                    and child.file_path not in seen):
                paths.append(child.file_path)
                seen.add(child.file_path)
        return paths

    def key(self, flow, event):
        if (self.window.mode != "gallery" or self.pane is not self.window.active or flow is not self.flow
                or event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                                  | Gdk.ModifierType.SUPER_MASK)):
            return False
        key = Gdk.keyval_name(event.keyval)
        if key in {"Tab", "ISO_Left_Tab"}:
            self.window.switch_browser_pane()
            return True
        if key not in {"Left", "Right", "Up", "Down", "Home", "End",
                       "KP_Left", "KP_Right", "KP_Up", "KP_Down", "KP_Home", "KP_End"}:
            return self.window.command_key(event)
        children = self.children_by_logical
        if not children:
            return True
        current = flow.get_focus_child()
        if current not in children:
            selected = flow.get_selected_children()
            current = selected[-1] if selected else next((item for item in children if item is not None), None)
        if current is None:
            if key.removeprefix("KP_") in {"Home", "End"}:
                self.pending_target = 0 if key.removeprefix("KP_") == "Home" else len(children) - 1
            return True
        index = current.gallery_index
        current_layout = (self.columns, self.capacity)
        new_layout = self.layout()
        capacity = new_layout[1]
        top = self.top()
        direction = key.removeprefix("KP_")
        self.pending_target = None
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
            next_top = self.max_top()
        elif target_index < top:
            next_top = max(0, min(target_index, top - capacity))
        elif target_index >= top + self.columns * capacity:
            next_top = min(self.max_top(),
                           max(top + capacity, target_index - self.columns * capacity + 1))
        else:
            next_top = top
        if new_layout != current_layout:
            self.show()
            self.pending_target = target_index
            self.pending_top = next_top
            return True
        self.scroll.get_vadjustment().set_value(
            next_top * geometry(flow, widget_children(flow))[1])
        target = self.child_for_index(target_index, next_top)
        if target is None:
            self.pending_target = target_index
            return True
        if not event.state & Gdk.ModifierType.SHIFT_MASK:
            flow.unselect_all()
        flow.select_child(target)
        target.grab_focus()
        self.scroll_to(target)
        return True

    def activated(self, flow, child):
        if self.window.mode != "gallery" or flow is not self.flow:
            return
        if getattr(child, "is_placeholder", False):
            return
        if child.is_directory:
            self.window.active.navigate(child.file_path)
        else:
            self.window.open_external(child.file_path)

    def show(self):
        if self.resize_source:
            GLib.source_remove(self.resize_source)
            self.resize_source = 0
        if self.scroll.get_allocated_width() <= 1:
            self.build_waiting = True
            return
        self.build_waiting = False
        same_folder = self.displayed_path == self.pane.path
        previous_top = self.top() if same_folder else 0
        self.columns, self.capacity = self.layout()
        self.serial += 1
        serial = self.serial
        self.building = True
        flow = self.flow
        old_children = widget_children(flow)
        focus = self.window.get_focus()
        focused_path = focus.file_path if focus in old_children and hasattr(focus, "file_path") else None
        should_focus_gallery = (self.pane is self.window.active and self.window.mode == "gallery"
                                and (not same_folder or focus is flow or focus in old_children))
        flow.set_min_children_per_line(self.columns)
        flow.set_max_children_per_line(self.columns)
        path = self.pane.path
        selected_gallery = ([child.file_path for child in flow.get_selected_children()
                             if hasattr(child, "file_path")]
                            if same_folder else [])
        restore_paths = set(selected_gallery) if selected_gallery else set(self.pane.selected_paths())
        self.displayed_path = path
        self.children_by_logical = []
        self.replicas_by_logical = {}
        self.preview_results = {}
        self.render_waiters = {}
        self.pending_target = None
        self.pending_top = None
        for child in old_children:
            flow.remove(child)
        if should_focus_gallery:
            flow.grab_focus()
        if not same_folder:
            self.scroll.get_vadjustment().set_value(0)
        self.pane.status.set_text("Loading previews…")
        if self.pane is self.window.active and self.window.mode == "gallery":
            self.window.gallery_flow = flow
            self.window.selection = None

        def loaded(rows):
            if serial != self.serial:
                return
            entries = [("..", True, 0, path.parent, True)] if path.parent != path else []
            entries += [(name, directory, size, path / name, False)
                        for name, directory, _symlink, size, _executable, _modified, _created in rows]
            restore_paths.intersection_update(entry[3] for entry in entries)
            self.children_by_logical = [None] * len(entries)
            ordered = []
            total_rows = (min(len(entries), max(self.capacity,
                          len(entries) - self.capacity))
                          if self.columns == 2 else len(entries))
            for row in range(total_rows):
                for column in range(self.columns):
                    logical = row + column * self.capacity
                    ordered.append((row, logical, entries[logical])
                                   if logical < len(entries) else (row, None, None))
            position = 0

            def add_batch():
                nonlocal position
                if serial != self.serial:
                    return False
                for row, logical, entry in ordered[position:position + 40]:
                    if logical is None:
                        self.add_placeholder()
                        continue
                    name, directory, size, target, parent = entry
                    child = self.add_tile(name, directory, size, target, parent, serial)
                    child.gallery_index = logical
                    child.gallery_row = row
                    if self.children_by_logical[logical] is None:
                        self.children_by_logical[logical] = child
                    self.replicas_by_logical.setdefault(logical, []).append(child)
                    if target in restore_paths:
                        flow.select_child(child)
                        if (should_focus_gallery and (focused_path is None or focused_path == target)
                                and previous_top <= row < previous_top + self.capacity):
                            child.grab_focus()
                    elif (not restore_paths and logical == 0
                          and self.pane is self.window.active and self.window.mode == "gallery"):
                        flow.select_child(child)
                        if should_focus_gallery:
                            child.grab_focus()
                    if logical == self.pending_target:
                        target_top = self.pending_top
                        if target_top is not None and target_top <= row < target_top + self.capacity:
                            flow.unselect_all()
                            flow.select_child(child)
                            if self.pane is self.window.active and self.window.mode == "gallery":
                                child.grab_focus()
                            self.scroll_to(child)
                            self.pending_target = None
                position += 40
                if position >= len(ordered):
                    self.building = False
                    flow.queue_resize()
                    self.scroll.queue_resize()
                    self.overlay.drawing_area.queue_draw()
                    _columns, pitch, _first_y = geometry(flow, widget_children(flow)) if widget_children(flow) else (1, 1, 0)
                    self.scroll.get_vadjustment().set_value(
                        min(self.pending_top if self.pending_top is not None else previous_top,
                            self.max_top()) * pitch)
                    selected = flow.get_selected_children()
                    if selected:
                        focused = next((item for item in selected if item.file_path == focused_path), selected[0])
                        target = self.child_for_index(focused.gallery_index, previous_top)
                        if target is not None:
                            self.scroll_to(target)
                            GLib.timeout_add(60, lambda: (self.scroll_to(target), False)[1]
                                             if serial == self.serial else False)
                    if should_focus_gallery:
                        if selected and target is not None:
                            target.grab_focus()
                        else:
                            flow.grab_focus()
                    elif focus is not None and focus.get_root() is self.window:
                        focus.grab_focus()
                    self.pending_top = None
                    self.resized()
                    self.window.refresh_file_footer(self.pane)
                    return False
                return True

            GLib.idle_add(add_batch)

        loaded(self.pane.listed_rows)

    def add_placeholder(self):
        child = Gtk.FlowBoxChild()
        child.is_placeholder = True
        child.set_size_request(310, 108)
        child.set_can_focus(False)
        child.set_sensitive(False)
        self.flow.insert(child, -1)
        child.show()

    def add_tile(self, name, directory, size, path, parent, serial):
        child = Gtk.FlowBoxChild()
        child.gallery_pane = self.pane
        child.file_path, child.is_directory, child.is_parent = path, directory, parent
        connect_key(child, lambda _child, event, flow=self.flow: self.key(flow, event))
        connect_click(child, lambda item, event: self.click(item, event, item))
        child.set_size_request(310, 108)
        if parent:
            parent_tile = Box(orientation=Gtk.Orientation.VERTICAL)
            parent_tile.add_css_class("gallery-tile")
            parent_label = Gtk.Label(label="..", xalign=0.5, yalign=0.5)
            parent_label.add_css_class("gallery-parent")
            parent_label.set_tooltip_text("Parent directory")
            parent_tile.pack_start(parent_label, True, True, 0)
            child.set_child(parent_tile)
            self.flow.insert(child, -1)
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
        kind_label = Gtk.Label(label="Folder" if directory else format_size(size), xalign=0)
        kind_label.add_css_class("muted")
        kind_label.set_ellipsize(Pango.EllipsizeMode.END)
        details.pack_start(kind_label, False, False, 0)
        extra_label = Gtk.Label(xalign=0)
        extra_label.add_css_class("muted")
        extra_label.set_ellipsize(Pango.EllipsizeMode.END)
        details.pack_start(extra_label, False, False, 0)
        tile.pack_start(details, True, True, 0)
        child.set_child(tile)
        self.flow.insert(child, -1)
        child.show()

        def render():
            if serial != self.serial:
                return None
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if directory:
                return None, None, "Folder", modified
            kind = Gio.content_type_guess(str(path), None)[0] or ""
            description = Gio.content_type_get_description(kind) or "File"
            summary = f"{description} · {format_size(size)}"
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
            if serial != self.serial or result is None:
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

        if path in self.preview_results:
            shown(self.preview_results[path])
        elif path in self.render_waiters:
            self.render_waiters[path].append(shown)
        else:
            self.render_waiters[path] = [shown]

            def delivered(result):
                if serial != self.serial:
                    return
                self.preview_results[path] = result
                for callback in self.render_waiters.pop(path, ()):
                    callback(result)

            def failed(_exc):
                if serial == self.serial:
                    self.render_waiters.pop(path, None)

            self.window.async_call(render, delivered, failed, executor=self.window.thumbnails_pool)
        return child
