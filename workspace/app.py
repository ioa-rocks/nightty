# SPDX-License-Identifier: MPL-2.0
"""GTK filesystem Surface. All slow IO is outside GTK's main thread."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import queue
import shlex
import socket
import stat

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte

from .agent_panel import AgentPanel
from .agents import Agents
from .appearance import load_appearance, stylesheet
from .model import context_for
from .pane import Pane
from .previews import format_size, read_preview
from .search import SearchCancelled, find_names
from .shells import ShellTabs
from .transfers import FileOperations
from .widgets import (Box, button, confirm, connect_key, dialog_button,
                      label, on_resize, scrolled, text_has_focus, textview,
                      widget_children)


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
        self.operations = FileOperations(self)
        self.shells = ShellTabs(self)
        self.context, self.context_serial, self.preview_serial = None, 0, 0
        self.project_context_path = None
        self.selection, self.mode = None, "files"
        self.quick_enabled, self.quick_serial = False, 0
        self.inspector_target, self.inspector_return_mode = None, "files"
        self.agent_panel = AgentPanel(self, client or Agents(args.backend))
        self.command_history = []
        self.command_history_index = None
        self.command_saved_input = ""
        self.command_terminal = None
        self.command_refresh_pending = False
        self.output_return_mode = "files"
        self.project_return_mode = "files"
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
        self.gallery_key_capture.connect("key-pressed", self.capture_browser_key)

        style = Gtk.CssProvider()
        style.load_from_data(stylesheet(self.appearance))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

        header = Gtk.HeaderBar(show_title_buttons=True)
        header.set_title_widget(Gtk.Label(label="Nightty"))
        self.tab_bar = Box(spacing=0)
        self.tab_bar.pack_start(self.shells.tab_bar, False, False, 0)
        header.pack_start(self.tab_bar)
        header.pack_end(self.operations.button)
        add_tab = button("+ Tab", self.shells.new)
        add_tab.set_tooltip_text("New shell tab · Ctrl+Shift+T")
        header.pack_end(add_tab)
        self.workspace_button = button("Shell ▾", self.show_workspace_menu)
        self.workspace_button.set_tooltip_text("Switch between Shell, Commander, and Project")
        header.pack_end(self.workspace_button)
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
            "sorts": (("name", False), ("name", False)),
            "active": 0,
        }
        self.panes = []
        self.active = None
        self.file_panes = None
        self.pane_split_ratio = .5
        self.pane_split_width = 0
        self.pane_split_updating = False
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
        self.project_tabs.append_page(self.agent_panel.widget, Gtk.Label(label="Agents"))
        self.project_tabs.connect("switch-page", lambda _n, _p, number: self.agent_panel.refresh() if number == 2 else None)
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
        self.terminal_box.pack_start(self.shells.notebook, True, True, 0)
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
        GLib.timeout_add(350, self.operations.update)
        GLib.timeout_add_seconds(1, self.refresh_tab_paths)

    def ensure_browser(self):
        if self.file_panes is not None:
            return
        browser = (self.shells.current.workspace_browser if self.shells.current is not None
                   else self.initial_browser_state)
        file_panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        file_panes.set_wide_handle(True)
        file_panes.add_css_class("file-split")
        on_resize(file_panes, self.resize_pane_split)
        file_panes.connect("notify::position", self.remember_pane_split)
        self.file_panes = file_panes
        self.stack.add_named(file_panes, "files")
        self.panes = [Pane(self, path, index) for index, path in enumerate(browser["paths"])]
        for pane in self.panes:
            self.refresh_pane_controls(pane)
        self.footer_height_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.VERTICAL)
        for pane in self.panes:
            self.footer_height_group.add_widget(pane.status_box)
        self.active = self.panes[browser["active"]]
        self.shells.browser_state_tab = self.shells.current
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
        self.quick_details_expander = details_expander
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
            if pane.gallery.flow is None:
                pane.gallery.build()
        self.gallery_flow = self.active.gallery.flow

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

    size = staticmethod(format_size)
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
            if self.shells.current is not None and not self.shells.restoring:
                self.shells.current.workspace_mode = self.mode
            if self.mode == "gallery":
                self.gallery_flow = pane.gallery.flow
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
            selected = (self.active.gallery.selected_paths() if self.mode == "gallery"
                        else pane.selected_paths())
            self.selection = selected[0] if selected else None
        return False

    def refresh_mode_buttons(self):
        mode_label = {"console": "Shell ▾", "project": "Project ▾"}.get(
            self.mode, "Commander ▾")
        if self.workspace_button.get_label() != mode_label:
            self.workspace_button.set_label(mode_label)
        for terminal, _pid in self.shells.terminals:
            control = getattr(terminal, "workspace_tab_button", None)
            if control is None:
                continue
            mode = (self.inspector_return_mode if self.mode == "inspect" else self.mode
                    ) if terminal is self.shells.current else terminal.workspace_mode
            if mode == "console":
                self.shells.update_cwd(terminal)
                folder = terminal.workspace_cwd
            elif (terminal is self.shells.current and self.active is not None
                  and self.shells.browser_state_tab is terminal):
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
            css.add_class("top-tab-active") if terminal is self.shells.current else css.remove_class("top-tab-active")

    def refresh_tab_paths(self):
        if self.get_visible():
            self.refresh_mode_buttons()
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
            return self.active.gallery.key(self.gallery_flow, event)
        self.active.view.grab_focus()
        return self.active.move_by_key(self.active.view, event)

    def directory_changed(self, pane):
        if not hasattr(self, "active") or not hasattr(self, "global_status"):
            return
        self.state.save(left=str(self.panes[0].path), right=str(self.panes[1].path))
        self.refresh_pane_controls(pane)
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
            selected = {str(path) for path in pane.gallery.selected_paths()}
            selected_child = pane.gallery.flow.get_focus_child()
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

    def refresh_pane_controls(self, pane):
        names = {"name": "Name", "extension": "Ext", "size": "Size",
                 "modified": "Modified", "created": "Created"}
        short = {"name": "n", "extension": "e", "size": "s",
                 "modified": "m", "created": "c"}
        sort_label = short[pane.sort_key].upper() if pane.sort_reverse else short[pane.sort_key]
        if pane.sort_button.get_label() != sort_label:
            pane.sort_button.set_label(sort_label)
        direction = "descending" if pane.sort_reverse else "ascending"
        pane.sort_button.set_tooltip_text(f"Sort: {names[pane.sort_key]}, {direction}; uppercase reverses order")
        pane.sort_button.update_property([Gtk.AccessibleProperty.LABEL],
                                         [f"Sort: {names[pane.sort_key]}, {direction}"])
        gallery = pane.content_stack.get_visible_child_name() == "gallery"
        view_icon = "view-grid-symbolic" if gallery else "view-list-symbolic"
        if pane.view_button.get_icon_name() != view_icon:
            pane.view_button.set_icon_name(view_icon)
        pane.view_button.set_tooltip_text(
            f"View: {'Gallery' if gallery else 'Files'}; choose Files, Gallery, or Info")
        pane.view_button.update_property([Gtk.AccessibleProperty.LABEL],
                                         [f"View: {'Gallery' if gallery else 'Files'}"])
        starred = Path(os.path.abspath(os.fspath(pane.path))) in self.state.favourites
        star_icon = "starred-symbolic" if starred else "non-starred-symbolic"
        if pane.locations_button.get_icon_name() != star_icon:
            pane.locations_button.set_icon_name(star_icon)
        pane.locations_button.set_tooltip_text(
            f"Locations and favourites; {'saved' if starred else 'not saved'} (Ctrl+D toggles)")
        pane.locations_button.update_property([Gtk.AccessibleProperty.LABEL],
            [f"Locations and favourites; current folder {'saved' if starred else 'not saved'}"])

    def refresh_star_buttons(self):
        for pane in self.panes:
            self.refresh_pane_controls(pane)

    def new_pane_menu(self, _pane, category):
        menu = Gtk.Popover()
        menu.set_has_arrow(True)
        menu.nightty_category = category
        menu.nightty_items = []
        shell = Box(orientation=Gtk.Orientation.VERTICAL, margin=9)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(480)
        content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        scroll.set_child(content)
        viewport = scroll.get_child()
        if isinstance(viewport, Gtk.Viewport):
            viewport.set_scroll_to_focus(True)
        shell.append(scroll)
        menu.nightty_scroller = scroll
        menu.nightty_content = content
        menu.set_child(shell)
        return menu, content

    @staticmethod
    def menu_choice(menu, content, title, callback):
        item = button(title, callback)
        item.add_css_class("pane-menu-item")
        item.set_halign(Gtk.Align.FILL)
        item.get_child().set_max_width_chars(46)
        item.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        item.get_child().set_xalign(0)
        content.append(item)
        menu.nightty_items.append(item)
        return item

    def build_locations_menu(self, pane):
        menu, content = self.new_pane_menu(pane, "location")
        current = Path(os.path.abspath(os.fspath(pane.path)))
        favourites = self.state.favourites
        recent = [path for path in self.state.recent_folders
                  if path != current and path not in favourites][:12]

        path_entry = Gtk.Entry()
        path_entry.set_text(str(current))
        path_entry.set_placeholder_text("Type a folder path")
        content.append(path_entry)

        def go(path):
            menu.popdown()
            pane.navigate(path)
            self.focus_active_browser()

        path_entry.connect("activate", lambda entry: go(Path(entry.get_text()).expanduser()))

        self.menu_choice(menu, content,
            "★  Remove current folder from favourites" if current in favourites
            else "☆  Add current folder to favourites",
            lambda: (menu.popdown(), pane.favourite(), self.focus_active_browser()))

        def section(title, paths):
            content.append(label(title, "section"))
            if not paths:
                content.append(label("None yet", "muted"))
            for name, path in paths:
                item = self.menu_choice(menu, content, name, lambda target=path: go(target))
                item.set_tooltip_text(str(path))

        section("Local", [("Home", Path.home()), ("Filesystem /", Path("/"))])
        mounts = []
        seen = {str(Path.home()), "/"}
        for mount in Gio.VolumeMonitor.get().get_mounts():
            root = mount.get_root()
            path = root.get_path()
            if path and path not in seen:
                seen.add(path)
                mounts.append((f"{mount.get_name()}  ·  {root.get_uri()}", Path(path)))
        if mounts:
            section("Mounted locations", mounts)
        section("Favourites", [(str(path), path) for path in favourites])
        section("Recent folders", [(str(path), path) for path in recent])
        return menu

    def build_sort_menu(self, pane):
        menu, content = self.new_pane_menu(pane, "sort")
        for key, title in (("name", "Name"), ("extension", "Extension"),
                           ("size", "Size"), ("modified", "Modified"),
                           ("created", "Created")):
            suffix = f"  {'↓' if pane.sort_reverse else '↑'}" if pane.sort_key == key else ""
            prefix = "●  " if pane.sort_key == key else "    "
            self.menu_choice(menu, content, prefix + title + suffix,
                lambda criterion=key: (menu.popdown(), pane.set_sort(criterion),
                                       self.focus_active_browser()))
        content.append(label("Select the current sort again to reverse it.", "muted"))
        content.append(label("Files without creation time appear last.", "muted"))
        return menu

    def build_view_menu(self, pane):
        menu, content = self.new_pane_menu(pane, "view")
        current = pane.content_stack.get_visible_child_name()
        for mode, title, shortcut in (("files", "Files", "Ctrl+1"),
                                      ("gallery", "Gallery", "Ctrl+I")):
            prefix = "●  " if current == mode else "    "
            self.menu_choice(menu, content, f"{prefix}{title}    {shortcut}",
                lambda target=mode: (menu.popdown(), self.select_pane_view(pane, target)))
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.menu_choice(menu, content, "Preview and info on right    Ctrl+5",
                         lambda: (menu.popdown(), self.show_quick_info()))
        self.menu_choice(menu, content, "Full details    F3",
                         lambda: (menu.popdown(), self.preview()))
        return menu

    def pane_menu_key(self, pane, menu, _anchor, _widget, event):
        key = Gdk.keyval_name(event.keyval)
        focus = self.get_focus()
        if key == "Escape":
            menu.popdown()
            self.focus_active_browser()
            return True
        categories = ("sort", "view", "location")
        if key in {"Left", "Right", "KP_Left", "KP_Right"}:
            if isinstance(focus, Gtk.Entry):
                return False
            step = -1 if "Left" in key else 1
            target = categories[(categories.index(menu.nightty_category) + step) % len(categories)]
            GLib.idle_add(lambda: (self.show_pane_menu(pane, target), False)[1])
            return True
        if key in {"Up", "Down", "KP_Up", "KP_Down"} and menu.nightty_items:
            items = menu.nightty_items
            step = -1 if "Up" in key else 1
            index = items.index(focus) if focus in items else (-1 if step > 0 else 0)
            target = items[(index + step) % len(items)]
            target.grab_focus()
            translated = target.translate_coordinates(menu.nightty_content, 0, 0)
            located, top = ((translated[0], translated[2]) if len(translated) == 3
                            else (True, translated[1]))
            adjustment = menu.nightty_scroller.get_vadjustment()
            if located and top is not None and adjustment.get_page_size() > 0:
                bottom = top + target.get_allocated_height()
                if top < adjustment.get_value():
                    adjustment.set_value(top)
                elif bottom > adjustment.get_value() + adjustment.get_page_size():
                    adjustment.set_value(bottom - adjustment.get_page_size())
            return True
        return False

    def discard_pane_menu(self, menu):
        anchor = getattr(menu, "nightty_anchor", None)
        menu.popdown()
        if menu.get_parent() is not None:
            menu.unparent()
        if anchor is not None and getattr(anchor, "nightty_popover", None) is menu:
            anchor.nightty_popover = None
        if self.active_pane_popover is menu:
            self.active_pane_popover = None

    def show_pane_menu(self, pane, category="location", anchor=None):
        if anchor is None and pane.index == 1 and self.quick_enabled and self.mode == "files":
            anchor = self.quick_dest_button
        anchor = anchor or {"sort": pane.sort_button, "location": pane.locations_button,
                            "view": pane.view_button}[category]
        if anchor is not getattr(self, "quick_dest_button", None):
            self.activate_pane(pane)
        menu = {"sort": self.build_sort_menu, "location": self.build_locations_menu,
                "view": self.build_view_menu}[category](pane)
        menu.nightty_anchor = anchor
        connect_key(menu, lambda widget, event: self.pane_menu_key(pane, menu, anchor, widget, event),
                    capture=True)
        previous_active = getattr(self, "active_pane_popover", None)
        if previous_active is not None:
            self.discard_pane_menu(previous_active)
        previous = getattr(anchor, "nightty_popover", None)
        if previous is not None and previous is not previous_active:
            self.discard_pane_menu(previous)
        anchor.nightty_popover = menu
        self.active_pane_popover = menu
        menu.set_parent(anchor)
        menu.popup()
        GLib.idle_add(lambda: (menu.nightty_items[0].grab_focus(), False)[1]
                      if menu.get_parent() is anchor and menu.nightty_items else False)
        return menu

    def show_locations(self, pane, anchor=None):
        if anchor is None and pane.index == 1 and self.quick_enabled and self.mode == "files":
            anchor = self.quick_dest_button
        return self.show_pane_menu(pane, "location", anchor)

    def show_quick_info(self):
        if self.mode != "files":
            self.set_mode("files")
        if self.active is not self.panes[0]:
            self.activate_pane(self.panes[0])
        if not self.quick_enabled:
            self.toggle_quick_view()
        self.quick_details_expander.set_expanded(True)

    def select_pane_view(self, pane, mode):
        if pane.index == 1 and self.quick_enabled:
            self.toggle_quick_view()
        self.activate_pane(pane)
        self.set_mode(mode)

    def show_workspace_menu(self):
        menu = Gtk.Popover()
        menu.nightty_items = []
        content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin=9)
        current = ("console" if self.mode == "console" else
                   "project" if self.mode == "project" else "files")
        for mode, title, shortcut in (("console", "Shell", "Ctrl+O"),
                                      ("files", "Commander", "Ctrl+1"),
                                      ("project", "Project", "Ctrl+P")):
            prefix = "●  " if mode == current else "    "
            def choose(target=mode):
                menu.popdown()
                if target == "files" and self.mode in {"files", "gallery"}:
                    self.focus_active_browser()
                elif target == "files":
                    return_mode = (self.project_previous_mode() if self.mode == "project"
                                   else self.output_return_mode)
                    self.set_mode(return_mode if return_mode in {"files", "gallery"} else "files")
                else:
                    self.set_mode(target)
            self.menu_choice(menu, content, f"{prefix}{title}    {shortcut}", choose)
        menu.set_child(content)
        previous = getattr(self.workspace_button, "nightty_popover", None)
        if previous is not None:
            previous.popdown()
            previous.unparent()
        self.workspace_button.nightty_popover = menu
        menu.set_parent(self.workspace_button)
        menu.popup()

    def add_favourite(self):
        self.state.toggle_favourite(self.active.path)
        self.refresh_star_buttons()

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
        if path is None:
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

        self.async_call(lambda: read_preview(path, 650, 650), shown,
            lambda exc: self.quick_document.get_buffer().set_text(f"Cannot preview {path.name}: {exc}") if serial == self.quick_serial else None)

    def project_previous_mode(self):
        terminal = self.shells.current
        return getattr(terminal, "workspace_project_return_mode", self.project_return_mode)

    def set_mode(self, mode, *, preserve_project_return=False):
        if mode not in {"files", "project", "console", "gallery", "inspect"}:
            mode = "files"
        previous_mode = self.mode
        if (mode in {"files", "gallery", "project"} and self.file_panes is None
                and self.shells.current is not None):
            terminal = self.shells.current
            self.shells.update_cwd(terminal)
            if terminal.workspace_cwd is not None:
                browser = terminal.workspace_browser
                paths = list(browser["paths"])
                paths[browser["active"]] = terminal.workspace_cwd
                browser["paths"] = tuple(paths)
        if mode == "gallery":
            self.ensure_gallery()
        elif mode == "files":
            self.ensure_browser()
        if (mode in {"files", "gallery"} and self.shells.current is not None
                and self.shells.browser_state_tab is not self.shells.current):
            self.shells.restore_browser_state(self.shells.current.workspace_browser)
            self.shells.browser_state_tab = self.shells.current
        previous_pane_mode = (self.active.content_stack.get_visible_child_name()
                              if self.active is not None else "files")
        terminal = self.shells.current
        if (mode == "project" and previous_mode != "project"
                and not self.shells.restoring and not preserve_project_return):
            return_mode = (self.inspector_return_mode if previous_mode == "inspect"
                           else previous_mode)
            self.project_return_mode = (return_mode if return_mode in {"files", "gallery", "console"}
                                        else "files")
            if terminal is not None:
                terminal.workspace_project_return_mode = self.project_return_mode
        if mode == "console" and previous_mode != "console" and not self.shells.restoring:
            self.output_return_mode = previous_mode
            if terminal is not None:
                terminal.workspace_output_return_mode = previous_mode
        self.mode = mode
        if mode not in {"files", "gallery"}:
            self.status.remove_css_class("footer-marked")
        if mode != "inspect":
            self.state.save(mode=mode)
            if terminal is not None and not self.shells.restoring:
                terminal.workspace_mode = mode
        self.stack.set_visible_child_name("files" if mode in {"files", "gallery"} else mode)
        self.command_row.show() if mode in {"files", "gallery"} else self.command_row.hide()
        self.info_box.show() if mode in {"project", "inspect"} else self.info_box.hide()
        if mode == "project":
            browser = terminal.workspace_browser if terminal is not None else self.initial_browser_state
            self.load_project_context(self.active.path if self.shells.browser_state_tab is terminal
                                      else browser["paths"][browser["active"]])
        if mode == "console":
            self.terminal_box.show()
            if not self.shells.terminals:
                self.shells.new(reveal=False)
            terminal = self.shells.terminals[self.shells.notebook.get_current_page()][0]
            terminal.grab_focus()
        else:
            if mode == "files":
                gallery_target = None
                if previous_pane_mode == "gallery":
                    flow = self.active.gallery.flow
                    focused = flow.get_focus_child()
                    selected_gallery = flow.get_selected_children()
                    item = (focused if focused is not None and hasattr(focused, "file_path")
                            else selected_gallery[-1] if selected_gallery else None)
                    gallery_target = item.file_path if item is not None else None
                self.active.content_stack.set_visible_child_name("files")
                if previous_pane_mode == "gallery":
                    self.active.gallery.serial += 1
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
                self.active.gallery.overlay.drawing_area.queue_draw()
                self.gallery_flow = self.active.gallery.flow
                if previous_pane_mode != "gallery":
                    self.active.gallery.show()
                else:
                    selected = self.active.gallery.selected_paths()
                    self.selection = selected[0] if selected else None
                self.update_command_prompt()
                self.refresh_folder_footer(self.active)
                self.refresh_file_footer(self.active)
                self.focus_active_browser()
        self.refresh_mode_buttons()
        for pane in self.panes:
            self.refresh_pane_controls(pane)
        if previous_mode != mode and mode in {"files", "gallery"}:
            GLib.idle_add(self.focus_browser_after_switch, mode)

    def focus_browser_after_switch(self, mode):
        if self.mode == mode and self.get_visible() and not text_has_focus(self.get_focus()):
            self.focus_active_browser()
        return False

    def focus_active_browser(self):
        if self.mode == "gallery":
            flow = self.active.gallery.flow
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

    def capture_browser_key(self, _controller, keyval, _keycode, state):
        focus = self.get_focus()
        key = Gdk.keyval_name(keyval)
        if self.shells.top_tab_shortcut(key, state):
            return True
        if focus is not None and (isinstance(focus, Gtk.Popover)
                                  or focus.get_ancestor(Gtk.Popover) is not None):
            return False
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
        return self.active.gallery.key(self.active.gallery.flow, argparse.Namespace(keyval=keyval, state=state))

    def preview(self, path=None):
        path = path or self.selection
        if path is None:
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
        self.async_call(lambda: read_preview(path, 1200, 860), show,
            lambda exc: self.document.get_buffer().set_text(f"Cannot inspect {path.name}: {exc}") if serial == self.preview_serial else None)

    def close_inspector(self):
        self.set_mode(self.inspector_return_mode)
        self.inspector_target = None

    def open_external(self, path=None):
        if (path is None and self.mode == "files" and self.active.results_active
                and not self.active.selected_paths()):
            return
        path = Path(path or (self.inspector_target if self.mode == "inspect" else self.selection or self.active.path))
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except GLib.Error as exc:
            self.status.set_text(f"Cannot open {path.name}: {exc}")

    def show_find_dialog(self, pane=None):
        pane = pane or self.active
        dialog = Gtk.Window(transient_for=self, modal=True)
        dialog.set_title("Find files")
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(640, -1)
        dialog.set_child(content)
        content.append(label("FIND FILES", "mc-dialog-title"))
        fields = Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fields.add_css_class("mc-transfer-fields")
        content.append(fields)
        fields.append(label("Filename contains", "mc-transfer-caption"))
        query_entry = Gtk.Entry()
        query_entry.add_css_class("mc-transfer-entry")
        query_entry.set_text(pane.results_query if pane.results_active else "")
        fields.append(query_entry)
        fields.append(label("Search in", "mc-transfer-caption"))
        scope = Gtk.DropDown.new_from_strings(["Current tree (live)", "All indexed files (plocate)"])
        scope.add_css_class("mc-find-scope")
        scope.set_selected(1 if pane.results_active and pane.results_scope == "indexed"
                           else 1 if self.state.data.get("find_scope") == "indexed" else 0)
        fields.append(scope)
        location = label(str(pane.path), "mc-dialog-detail")
        content.append(location)
        error = label("", "mc-transfer-error")
        error.set_visible(False)
        content.append(error)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        find = Gtk.Button(label="_Find", use_underline=True)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        for choice in (find, cancel):
            dialog_button(choice)
            actions.append(choice)
        dialog.set_default_widget(find)

        def begin():
            query = query_entry.get_text().strip()
            if not query:
                error.set_text("Enter a filename to find.")
                error.set_visible(True)
                query_entry.grab_focus()
                return
            chosen = "indexed" if scope.get_selected() == 1 else "tree"
            self.state.save(find_scope=chosen)
            dialog.destroy()
            self.start_find(pane, query, chosen)

        find.connect("clicked", lambda *_: begin())
        cancel.connect("clicked", lambda *_: dialog.destroy())
        query_entry.connect("activate", lambda *_: begin())
        connect_key(dialog, lambda _widget, event: (dialog.destroy(), True)[1]
                    if event.keyval == Gdk.KEY_Escape else False, capture=True)
        dialog.present()
        query_entry.grab_focus()
        query_entry.select_region(0, -1)
        return dialog

    def repeat_find(self, pane, reveal=False):
        if pane.results_active:
            self.start_find(pane, pane.results_query, pane.results_scope,
                            preserve_marks=True, reveal=reveal)

    def start_find(self, pane, query, scope="tree", preserve_marks=False, reveal=True):
        if reveal:
            if pane is not self.active:
                self.activate_pane(pane)
            if self.mode != "files":
                self.set_mode("files")
        serial, cancellation = pane.begin_results(query, scope, preserve_marks, focus=reveal)
        requested_created = pane.sort_key == "created"
        batches = queue.SimpleQueue()
        search_finished = False

        def drain():
            if search_finished or serial != pane.results_serial or not pane.results_active:
                return False
            changed = False
            while True:
                try:
                    pane.results_matches.extend(batches.get_nowait())
                    changed = True
                except queue.Empty:
                    break
            if changed:
                pane.show_result_rows(loading=True)
            return True

        GLib.timeout_add(120, drain)

        def finish(outcome):
            nonlocal search_finished
            if serial != pane.results_serial or not pane.results_active:
                return
            drain()
            search_finished = True
            pane.results_created_loaded = requested_created
            pane.show_result_rows(outcome=outcome)

        def failed(exc):
            nonlocal search_finished
            if serial != pane.results_serial or not pane.results_active:
                return
            drain()
            search_finished = True
            if not isinstance(exc, SearchCancelled):
                pane.show_result_rows(error=str(exc))

        self.async_call(lambda: find_names(query, pane.path, scope, pane.hidden, batches.put,
                                           cancellation.is_set,
                                           include_created=requested_created), finish, failed,
                        executor=self.listing_pool)

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
            if self.shells.has_foreground_program(terminal):
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
                self.shells.flush_commands(terminal)
            self.shells.notebook.set_current_page(terminal.workspace_page_index)
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
        if self.shells.has_foreground_program(terminal):
            self.command_entry.set_text(command)
            self.status.set_text("Shell is busy; wait for the prompt or use its terminal view.")
            return
        payload = (f"cd {shlex.quote(str(directory))} && {command}\n"
                   if terminal.workspace_cwd != directory else command + "\n")
        terminal.workspace_pending.append(payload)
        terminal.workspace_cwd = directory
        self.shells.flush_commands(terminal)
        self.command_refresh_pending = True
        self.set_mode("console")
        self.shells.notebook.set_current_page(terminal.workspace_page_index)
        terminal.grab_focus()

    def shell_for_commands(self):
        terminal = self.shells.current
        if terminal is None or terminal.workspace_exited:
            terminal = self.shells.new()
        self.shells.update_cwd(terminal)
        self.command_terminal = terminal
        return terminal

    def toggle_shell_output(self):
        if self.mode == "console":
            return_mode = (self.shells.current.workspace_output_return_mode
                           if self.shells.current is not None else self.output_return_mode)
            self.set_mode(return_mode if return_mode != "console" else "files",
                          preserve_project_return=True)
        else:
            self.set_mode("console")

    def mode_shortcut(self, key):
        key = (key or "").lower()
        if key == "o":
            self.toggle_shell_output()
        elif key == "p":
            self.set_mode(self.project_previous_mode() if self.mode == "project" else "project")
        elif key == "i":
            self.set_mode("files" if self.mode == "gallery" else "gallery")
        else:
            return False
        return True

    def hide_window(self, *_):
        self.hide()
        return True

    def quit(self, confirmed=False, on_cancel=None):
        active_terminals = any(pid for _terminal, pid in self.shells.terminals)
        active_jobs = bool(self.operations.running())
        if (active_terminals or active_jobs) and not confirmed:
            question = ("Quit Nightty and close its terminals?" if active_terminals
                        else "Quit Nightty and cancel unfinished file operations?")
            confirm(self, question,
                    "Hiding keeps everything running. Quitting closes embedded terminals and cancels unfinished file operations; completed changes are kept.",
                    lambda: self.quit(confirmed=True), on_cancel)
            return False
        self.operations.shutdown()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.listing_pool.shutdown(wait=False, cancel_futures=True)
        self.thumbnails_pool.shutdown(wait=False, cancel_futures=True)
        self.app.quit()
        return True

    def key(self, _widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.ALT_MASK)
        key = Gdk.keyval_name(event.keyval)
        terminal_focus = any(terminal.has_focus() for terminal, _pid in self.shells.terminals)
        focus = self.get_focus()
        if (focus is not None and focus.get_ancestor(Gtk.Popover) is not None
                and key in {"Escape", "Left", "Right", "Up", "Down", "Return", "KP_Enter"}):
            return False
        if self.shells.top_tab_shortcut(key, event.state):
            return True
        if terminal_focus and not (
                ctrl and (not shift and key.lower() in {"o", "p", "q", "1", "2", "3", "4"}
                          or shift and key.lower() in {"o", "p", "t", "c", "v"})
                or key == "F12"):
            return False
        if terminal_focus and ctrl and shift and key.lower() in {"o", "p"}:
            self.shells.current.feed_child(
                b"\x0f" if key.lower() == "o" else b"\x10")
            return True
        if ((terminal_focus or self.mode == "console") and ctrl and not shift
                and key.lower() in {"o", "p", "1", "2", "3", "4"}
                and self.shells.has_foreground_program(self.shells.current)):
            return False
        if ctrl and key in {"1", "2", "3", "4"}:
            self.set_mode({"1": "files", "2": "project", "3": "console", "4": "gallery"}[key])
        elif ctrl and not shift and not alt and self.mode_shortcut(key):
            pass
        elif (ctrl and not shift and not alt and key in {"F1", "F2"}
              and self.mode in {"files", "gallery"}):
            self.show_pane_menu(self.panes[0 if key == "F1" else 1], "location")
        elif ctrl and key == "5":
            self.toggle_quick_view()
        elif alt and key in {"1", "2", "F1", "F2"}:
            if self.mode != "files":
                self.set_mode("files")
            self.show_locations(self.panes[0 if key in {"1", "F1"} else 1])
        elif alt and key.lower() == "s":
            self.active.show_search()
        elif (alt and key == "F7" or ctrl and shift and key.lower() == "f") and self.mode in {"files", "gallery"}:
            self.show_find_dialog()
        elif ctrl and shift and key.lower() == "t":
            self.shells.new()
        elif terminal_focus and ctrl and shift and key.lower() in {"c", "v"}:
            index = self.shells.notebook.get_current_page()
            terminal = self.shells.terminals[index][0]
            if key.lower() == "c":
                terminal.copy_clipboard_format(Vte.Format.TEXT)
            else:
                self.shells.paste_clipboard(terminal)
        elif ctrl and key.lower() == "q":
            self.quit()
        elif key == "F12":
            self.hide()
        elif key == "Escape" and self.mode == "inspect" and not terminal_focus:
            self.close_inspector()
        elif key == "Escape" and self.mode == "files" and self.active.search_row.get_visible():
            self.active.hide_search()
        elif key == "Escape" and self.mode == "files" and self.active.results_active:
            self.active.close_results()
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
            self.operations.copy(same_folder=shift)
        elif key == "F6" and self.mode in {"files", "gallery"}:
            self.operations.move(same_folder=shift)
        elif key == "F7" and self.mode in {"files", "gallery"}:
            self.operations.create_directory()
        elif key == "F8" and self.mode in {"files", "gallery"}:
            self.operations.delete()
        else:
            return False
        return True
