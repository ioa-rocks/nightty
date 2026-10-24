"""GTK filesystem Surface. All slow IO is outside GTK's main thread."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, Vte

from .agents import Agents
from .model import State, Transfer, context_for, directory_entries

CSS = b"""
window { background:#151a22; color:#d9e0eb; }
headerbar { background:#1b2330; border-bottom:1px solid #303b4b; }
button { border-radius:5px; padding:5px 10px; }
entry { background:#111720; color:#d9e0eb; border-color:#354155; }
treeview { background:#171e29; color:#d8e0eb; }
treeview:selected { background:#344f6d; }
treeview header button { background:#202b3b; color:#90a1b9; }
notebook header { background:#1b2330; }
textview, textview text { background:#141b25; color:#dce5f1; }
.sidebar { background:#111720; }
.sidebar button { background:transparent; border:none; color:#b3c2d6; }
.muted { color:#8b9bb1; font-size:12px; }
.heading { font-size:22px; font-weight:600; color:#eef3fb; }
.section { font-size:11px; font-weight:600; color:#7b96b7; }
.context { background:#1e2d3c; padding:10px 14px; border-radius:6px; }
.footer { background:#10161e; padding:8px 12px; color:#8eabc8; font-size:12px; }
.pane-active { border-top:2px solid #7fb0d7; }
.pane-inactive { border-top:2px solid #263344; }
"""


def label(text="", css=None):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_selectable(True)
    widget.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def button(text, callback):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda *_: callback())
    return widget


def scrolled(widget):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(widget)
    return scroll


def textview():
    view = Gtk.TextView(editable=False, cursor_visible=False,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.set_left_margin(24)
    view.set_right_margin(24)
    view.set_top_margin(20)
    view.set_bottom_margin(20)
    return view


class Pane(Gtk.Box):
    def __init__(self, window, folder, index):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window, self.path, self.index = window, Path(folder), index
        self.hidden, self.generation = False, 0
        toolbar = Gtk.Box(spacing=5, margin=7)
        toolbar.pack_start(button("↑", self.up), False, False, 0)
        self.location = Gtk.Entry()
        self.location.set_text(str(self.path))
        self.location.connect("activate", lambda entry: self.navigate(Path(entry.get_text()).expanduser()))
        toolbar.pack_start(self.location, True, True, 0)
        toolbar.pack_start(button("☆", self.favourite), False, False, 0)
        self.pack_start(toolbar, False, False, 0)
        self.store = Gtk.ListStore(str, str, str, str)
        self.view = Gtk.TreeView(model=self.store, headers_visible=True)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for index, (title, expand) in enumerate((("", False), ("Name", True), ("Size", False))):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_expand(expand)
            self.view.append_column(column)
        self.view.connect("focus-in-event", lambda *_: self.window.activate_pane(self))
        self.view.connect("row-activated", self.open)
        self.view.connect("cursor-changed", self.selected)
        self.pack_start(scrolled(self.view), True, True, 0)
        self.count = label("Loading…", "muted")
        self.count.set_margin_start(10)
        self.count.set_margin_top(7)
        self.count.set_margin_bottom(7)
        self.pack_start(self.count, False, False, 0)
        self.navigate(self.path)

    def navigate(self, path):
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = self.path / path
        path = path.absolute()
        self.generation += 1
        generation = self.generation
        self.count.set_text("Loading…")

        def loaded(rows):
            if generation != self.generation:
                return
            self.path = path
            self.location.set_text(str(path))
            self.store.clear()
            if path.parent != path:
                self.store.append(["↰", "..", "", str(path.parent)])
            for name, directory, symlink, size in rows:
                self.store.append(["▸" if directory else "↗" if symlink else "·", name,
                    "" if directory else self.window.size(size), str(path / name)])
            self.count.set_text(f"{len(rows)} entries" + (" · hidden included" if self.hidden else ""))
            self.window.directory_changed(self)

        self.window.async_call(lambda: directory_entries(path, self.hidden), loaded,
            lambda exc: self.count.set_text(f"Cannot open {path}: {exc}"))

    def up(self):
        self.navigate(self.path.parent)

    def favourite(self):
        self.window.state.toggle_favourite(self.path)
        self.window.refresh_favourites()

    def selected_paths(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [Path(model[path][3]) for path in paths if model[path][1] != ".."]

    def selected(self, *_):
        self.window.activate_pane(self, update=getattr(self.window, "active", self) is not self)
        selected = self.selected_paths()
        if selected:
            self.window.selection = selected[0]
            self.window.status.set_text(str(selected[0]))

    def open(self, _view, treepath, _column):
        path = Path(self.store[treepath][3])
        if path.is_dir():
            self.navigate(path)
        else:
            self.window.preview(path)
            self.window.set_mode("project")


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, args, state, client=None):
        super().__init__(application=app, title="Workspace")
        self.app, self.args, self.state = app, args, state
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="surface-io")
        self.transfers_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="surface-copy")
        self.jobs, self.terminals = [], []
        self.context, self.context_serial, self.preview_serial = None, 0, 0
        self.selection, self.mode = None, "files"
        self.agent_client = client or Agents(args.backend)
        self.agent_snapshot, self.agent_session = {}, None
        self.agent_loading, self.agent_sending, self.agent_request_key = False, False, None
        self.agent_drafts = {}
        self.pending_agent_sends = {}
        self.set_default_size(1400, 900)
        self.set_size_request(760, 500)
        self.connect("delete-event", self.hide_window)
        self.connect("key-press-event", self.key)

        style = Gtk.CssProvider()
        style.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

        header = Gtk.HeaderBar(show_close_button=True, title="Workspace")
        header.set_subtitle("Files · projects · terminals")
        self.mode_buttons = {}
        for name, title in (("files", "Files"), ("project", "Project"), ("console", "Console")):
            control = button(title, lambda value=name: self.set_mode(value))
            self.mode_buttons[name] = control
            header.pack_start(control)
        header.pack_end(button("Terminal", self.toggle_terminal))
        header.pack_end(button("Transfers", lambda: self.show_jobs()))
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        body = Gtk.Box()
        outer.pack_start(body, True, True, 0)
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        self.sidebar.set_size_request(175, -1)
        self.sidebar.get_style_context().add_class("sidebar")
        self.sidebar.pack_start(label("FAVOURITES", "section"), False, False, 6)
        self.favourite_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.sidebar.pack_start(self.favourite_box, False, False, 0)
        self.sidebar.pack_start(button("+ Current folder", self.add_favourite), False, False, 4)
        self.sidebar.pack_end(label("Ctrl+1  Files\nCtrl+2  Project\nCtrl+3  Console\nCtrl+`  Terminal\nF5  Background copy", "muted"), False, False, 10)
        body.pack_start(self.sidebar, False, False, 0)

        self.vertical = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        body.pack_start(self.vertical, True, True, 0)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self.vertical.pack1(self.stack, resize=True, shrink=False)
        file_panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.stack.add_named(file_panes, "files")
        left = args.left or state.data.get("left") or str(Path.cwd())
        right = args.right or state.data.get("right") or left
        self.panes = [Pane(self, left, 0), Pane(self, right, 1)]
        self.active = self.panes[0]
        file_panes.pack1(self.panes[0], resize=True, shrink=False)
        file_panes.pack2(self.panes[1], resize=True, shrink=False)
        file_panes.set_position(510)

        project = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
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
        self.preview_stack = Gtk.Stack()
        self.document = textview()
        self.preview_stack.add_named(scrolled(self.document), "text")
        self.image = Gtk.Image()
        self.preview_stack.add_named(scrolled(self.image), "image")
        self.project_tabs.append_page(self.preview_stack, Gtk.Label(label="Preview"))
        self.work_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=20)
        self.project_tabs.append_page(scrolled(self.work_box), Gtk.Label(label="Work"))
        self.agents_box = self.build_agents()
        self.project_tabs.append_page(self.agents_box, Gtk.Label(label="Agents"))
        self.project_tabs.connect("switch-page", lambda _n, _p, number: self.refresh_agents() if number == 3 else None)
        self.stack.add_named(project, "project")

        self.terminal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar = Gtk.Box(spacing=5, margin=6)
        toolbar.pack_start(label("TERMINALS", "section"), False, False, 8)
        toolbar.pack_start(button("+ Shell", self.new_terminal), False, False, 0)
        toolbar.pack_start(button("+ MC", lambda: self.new_terminal(mc=True)), False, False, 0)
        toolbar.pack_end(button("Hide", self.toggle_terminal), False, False, 0)
        self.terminal_box.pack_start(toolbar, False, False, 0)
        self.terminal_tabs = Gtk.Notebook(scrollable=True)
        self.terminal_box.pack_start(self.terminal_tabs, True, True, 0)
        self.vertical.pack2(self.terminal_box, resize=True, shrink=False)
        self.vertical.set_position(500)

        self.status = label("Loading workspace…", "footer")
        outer.pack_start(self.status, False, False, 0)
        self.refresh_favourites()
        self.show_all()
        self.terminal_box.hide()
        self.activate_pane(self.active)
        self.set_mode(state.data.get("mode", "files"))
        GLib.timeout_add(350, self.update_transfers)

    @staticmethod
    def size(value):
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024 or unit == "GiB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024

    def async_call(self, operation, callback, error=None):
        future = self.pool.submit(operation)

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
        self.active = pane
        for candidate in self.panes:
            css = candidate.get_style_context()
            css.remove_class("pane-active")
            css.remove_class("pane-inactive")
            css.add_class("pane-active" if candidate is pane else "pane-inactive")
        if update:
            self.directory_changed(pane)
        return False

    def directory_changed(self, pane):
        if not hasattr(self, "active") or not hasattr(self, "status"):
            return
        self.state.save(left=str(self.panes[0].path), right=str(self.panes[1].path))
        if pane is not self.active:
            return
        self.selection = None
        self.context_serial += 1
        serial = self.context_serial
        path = pane.path

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
            self.work_box.show_all()
            self.status.set_text(str(path))
        self.async_call(load, loaded)

    def refresh_favourites(self):
        for child in self.favourite_box.get_children():
            self.favourite_box.remove(child)
        values = self.state.favourites
        if not values:
            self.favourite_box.pack_start(label("Star a folder to keep it here.", "muted"), False, False, 6)
        for path in values:
            control = button("☆  " + path.name, lambda p=path: self.active.navigate(p))
            control.set_tooltip_text(str(path))
            self.favourite_box.pack_start(control, False, False, 0)
        self.favourite_box.show_all()

    def add_favourite(self):
        self.state.toggle_favourite(self.active.path)
        self.refresh_favourites()

    def set_mode(self, mode):
        if mode not in {"files", "project", "console"}:
            mode = "files"
        self.mode = mode
        self.state.save(mode=mode)
        if mode == "console":
            self.stack.hide()
            self.terminal_box.show_all()
            if not self.terminals:
                self.new_terminal()
            self.terminals[-1][0].grab_focus()
        else:
            self.stack.show()
            self.stack.set_visible_child_name(mode)
            if mode == "files":
                self.active.view.grab_focus()
        for name, control in self.mode_buttons.items():
            css = control.get_style_context()
            css.add_class("suggested-action") if name == mode else css.remove_class("suggested-action")

    def preview(self, path=None):
        path = path or self.selection
        if path is None or path.is_dir():
            return
        self.preview_serial += 1
        serial = self.preview_serial
        self.document.get_buffer().set_text("Loading " + str(path))
        self.preview_stack.set_visible_child_name("text")
        self.project_tabs.set_current_page(1)
        self.set_mode("project")

        def read():
            kind, _ = Gio.content_type_guess(str(path), None)
            if kind and kind.startswith("image/"):
                return "image", GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 1300, 900, True)
            with path.open("rb") as stream:
                content = stream.read(300001)
            if b"\0" in content:
                return "text", f"{path.name}\n\nBinary file · {self.size(path.stat().st_size)}\nUse Open (F4) to view it in its application."
            text = content[:300000].decode("utf-8", errors="replace")
            return "text", text + ("\n\n[Preview truncated]" if len(content) > 300000 else "")

        def show(value):
            if serial != self.preview_serial:
                return
            kind, result = value
            if kind == "image":
                self.image.set_from_pixbuf(result)
            else:
                self.document.get_buffer().set_text(result)
            self.preview_stack.set_visible_child_name(kind)
            self.status.set_text(str(path))
        self.async_call(read, show, lambda exc: self.document.get_buffer().set_text(f"Cannot preview {path.name}: {exc}"))

    def open_external(self):
        path = self.selection or self.active.path
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def copy(self):
        sources = self.active.selected_paths()
        if not sources:
            self.status.set_text("Select files or folders to copy (Ctrl/Shift selects several).")
            return
        destination = self.panes[1 - self.active.index].path
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL, text=f"Copy {len(sources)} selected item(s) in the background?")
        dialog.format_secondary_text(str(destination) + "\nExisting destinations will be left unchanged.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.start_copy(sources, destination)

    def start_copy(self, sources, destination):
        job = Transfer(sources, destination)
        self.jobs.append(job)
        self.transfers_pool.submit(job.run)
        self.status.set_text("Copy queued; you can keep navigating.")
        return job

    def update_transfers(self):
        running = [job for job in self.jobs if job.status in {"queued", "copying"}]
        for job in self.jobs:
            if job.status in {"completed", "failed", "cancelled"} and not getattr(job, "announced", False):
                job.announced = True
                for pane in self.panes:
                    if pane.path.resolve() == job.destination.resolve():
                        pane.navigate(pane.path)
                self.status.set_text(f"Copy {job.status}: {job.files} files, {self.size(job.bytes)}" + (f" · {job.error}" if job.error else ""))
        if running:
            self.status.set_text(f"{len(running)} transfer(s) · " + " · ".join(f"{job.files} files / {self.size(job.bytes)}" for job in running))
        if hasattr(self, "jobs_label") and self.jobs_label.get_parent():
            self.jobs_label.set_text("\n\n".join(
                f"{job.status.upper()}   {job.files} files · {self.size(job.bytes)}\n{job.destination}\n{job.error}" for job in self.jobs) or "No transfers yet.")
        return True

    def show_jobs(self):
        dialog = Gtk.Dialog(title="Background transfers", transient_for=self, flags=0)
        dialog.set_default_size(620, 380)
        self.jobs_label = label("")
        self.jobs_label.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.jobs_label.set_line_wrap(True)
        self.jobs_label.set_margin_start(20)
        self.jobs_label.set_margin_top(15)
        dialog.get_content_area().pack_start(scrolled(self.jobs_label), True, True, 0)
        dialog.add_button("Cancel running transfers", Gtk.ResponseType.REJECT)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, response: ([(job.cancel.set()) for job in self.jobs if job.status in {"queued", "copying"}] if response == Gtk.ResponseType.REJECT else None, d.destroy()))
        self.update_transfers()
        dialog.show_all()

    def new_terminal(self, mc=False, command=None):
        terminal = Vte.Terminal()
        terminal.set_scrollback_lines(20000)
        terminal.set_font(Pango.FontDescription("Monospace 11"))
        background = Gdk.RGBA()
        background.parse("#10161e")
        terminal.set_color_background(background)
        terminal.set_audible_bell(False)
        terminal.set_scroll_on_keystroke(True)
        terminal.connect("button-press-event", self.terminal_menu)
        folder = self.active.path
        argv = command or (["mc"] if mc else [os.environ.get("SHELL", "/bin/bash"), "-i"])
        title = Gtk.Label(label=("MC · " if mc else "Shell · ") + folder.name)
        page = scrolled(terminal)
        index = self.terminal_tabs.append_page(page, title)
        record = [terminal, 0]
        self.terminals.append(record)
        terminal.connect("child-exited", lambda *_: (record.__setitem__(1, 0), title.set_text(title.get_text() + " · exited")))

        def spawned(_terminal, pid, error, _data):
            record[1] = max(0, pid)
            if error:
                self.failure(error)
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(folder), argv, None,
            GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, spawned, None)
        self.terminal_box.show_all()
        self.terminal_tabs.set_current_page(index)
        terminal.grab_focus()
        return terminal

    def terminal_menu(self, terminal, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        for title, callback in (("Copy", lambda: terminal.copy_clipboard_format(Vte.Format.TEXT)),
                                ("Paste", terminal.paste_clipboard)):
            item = Gtk.MenuItem(label=title)
            item.connect("activate", lambda _item, action=callback: action())
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def toggle_terminal(self):
        if self.mode == "console":
            self.set_mode("files")
            self.terminal_box.hide()
        elif self.terminal_box.get_visible():
            self.terminal_box.hide()
            self.active.view.grab_focus()
        else:
            self.terminal_box.show_all()
            if not self.terminals:
                self.new_terminal()
            else:
                index = self.terminal_tabs.get_current_page()
                self.terminals[index][0].grab_focus()

    def build_agents(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=15)
        controls = Gtk.Box(spacing=8)
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

    def quit(self):
        active = any(pid for _terminal, pid in self.terminals) or any(j.status in {"queued", "copying"} for j in self.jobs)
        if active:
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, buttons=Gtk.ButtonsType.OK_CANCEL,
                message_type=Gtk.MessageType.QUESTION, text="Quit Workspace and close its terminals?")
            dialog.format_secondary_text("Hiding keeps everything running. Quitting closes embedded terminals and cancels unfinished copies; completed files are kept.")
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                return
        for job in self.jobs:
            job.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.transfers_pool.shutdown(wait=False, cancel_futures=True)
        self.app.quit()

    def key(self, _widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        key = Gdk.keyval_name(event.keyval)
        terminal_focus = any(terminal.has_focus() for terminal, _pid in self.terminals)
        if ctrl and key in {"1", "2", "3"}:
            self.set_mode({"1": "files", "2": "project", "3": "console"}[key])
        elif ctrl and key in {"grave", "asciitilde"}:
            self.toggle_terminal()
        elif ctrl and shift and key.lower() == "t":
            self.new_terminal()
        elif terminal_focus and ctrl and shift and key.lower() in {"c", "v"}:
            index = self.terminal_tabs.get_current_page()
            terminal = self.terminals[index][0]
            if key.lower() == "c":
                terminal.copy_clipboard_format(Vte.Format.TEXT)
            else:
                terminal.paste_clipboard()
        elif ctrl and key.lower() == "q":
            self.quit()
        elif key == "F12" or key == "Escape" and not terminal_focus:
            self.hide()
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
        elif key == "Tab" and self.mode == "files" and self.active.view.has_focus():
            self.activate_pane(self.panes[1 - self.active.index])
            self.active.view.grab_focus()
        elif key == "F3":
            self.preview()
        elif key == "F4":
            self.open_external()
        elif key == "F5" and self.mode == "files":
            self.copy()
        else:
            return False
        return True
