# SPDX-License-Identifier: MPL-2.0
"""Top-bar shell tabs: one VTE terminal per tab, each remembering its view and browser folders."""

import os
from pathlib import Path
import re
import signal
from urllib.parse import urlsplit

from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte

from .widgets import Box, button, confirm, connect_click, connect_key, scrolled


class ShellTabs:
    """Owns the terminal records, the notebook of terminals and the tab buttons in the header.

    ``terminals`` is a list of ``[terminal, pid]`` records in tab order. The tab a user is on is
    ``current``; ``browser_state_tab`` is the tab whose folders the file panes currently show, so
    switching tabs can capture one tab's browser state and restore another's.
    """

    def __init__(self, window):
        self.window = window
        self.terminals = []
        self.sequence = 0
        self.current = None
        self.restoring = False
        self.browser_state_tab = None
        self.tab_bar = Box(spacing=0)
        self.notebook = Gtk.Notebook(scrollable=True)
        self.notebook.set_show_tabs(False)
        self.notebook.connect("switch-page", self.page_changed)

    def page_changed(self, _notebook, _page, _number):
        self.window.refresh_mode_buttons()

    def select(self, index):
        if index < 0 or index >= len(self.terminals):
            return
        target = self.terminals[index][0]
        previous = self.current
        if previous is not None and previous is not target:
            previous.workspace_mode = self.window.inspector_return_mode if self.window.mode == "inspect" else self.window.mode
            if self.browser_state_tab is previous:
                previous.workspace_browser = self.capture_browser_state()
        self.current = target
        self.notebook.set_current_page(index)
        self.restoring = True
        try:
            if previous is not target and target.workspace_mode in {"files", "gallery"}:
                self.restore_browser_state(target.workspace_browser)
                if self.window.panes:
                    self.browser_state_tab = target
            self.window.set_mode(target.workspace_mode)
        finally:
            self.restoring = False
        self.window.refresh_mode_buttons()
        if target.workspace_mode == "console":
            target.grab_focus()

    def capture_browser_state(self):
        if not self.window.panes:
            return dict(self.window.initial_browser_state)
        return {
            "paths": tuple(pane.path for pane in self.window.panes),
            "pane_modes": tuple(pane.content_stack.get_visible_child_name() for pane in self.window.panes),
            "sorts": tuple((pane.sort_key, pane.sort_reverse) for pane in self.window.panes),
            "active": self.window.active.index,
        }

    def restore_browser_state(self, browser):
        if not self.window.panes:
            return
        for pane, path, mode, sorting in zip(self.window.panes, browser["paths"], browser["pane_modes"],
                                            browser.get("sorts", (("name", False),) * 2)):
            pane.sort_key, pane.sort_reverse = sorting
            if pane.path != path or (pane.sort_key == "created" and not pane.created_loaded):
                pane.navigate(path)
            elif pane.listed_rows and not pane.results_active:
                pane.show_directory_rows(pane.listed_rows)
            if mode == "gallery":
                self.window.ensure_gallery()
            pane.content_stack.set_visible_child_name(mode)
            self.window.refresh_pane_controls(pane)
            if mode == "gallery":
                pane.gallery.show()
        self.window.activate_pane(self.window.panes[browser["active"]], update=False)
        self.window.directory_changed(self.window.active)

    def switch(self, direction):
        tabs = [terminal for terminal, _pid in self.terminals]
        if not tabs:
            return
        current = self.current or tabs[0]
        target = tabs[(tabs.index(current) + direction) % len(tabs)]
        self.select(next(i for i, (terminal, _pid) in enumerate(self.terminals) if terminal is target))

    def top_tab_shortcut(self, key, state):
        if not state & Gdk.ModifierType.CONTROL_MASK or state & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if key in {"Page_Up", "KP_Page_Up", "Prior", "KP_Prior"}:
            self.switch(-1)
        elif key in {"Page_Down", "KP_Page_Down", "Next", "KP_Next"}:
            self.switch(1)
        else:
            return False
        return True

    def update_cwd(self, terminal):
        if (not terminal.workspace_ready or terminal.workspace_pending
                or self.has_foreground_program(terminal)):
            return
        record = next((record for record in self.terminals if record[0] is terminal), None)
        if record is None or not record[1]:
            return
        try:
            terminal.workspace_cwd = Path(os.readlink(f"/proc/{record[1]}/cwd"))
        except OSError:
            pass

    @staticmethod
    def flush_commands(terminal):
        if terminal.workspace_ready and not terminal.workspace_exited and terminal.workspace_pending:
            terminal.workspace_ready = False
            terminal.feed_child(terminal.workspace_pending.pop(0).encode("utf-8"))

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
            self.paste_clipboard(_terminal)
            return True
        if control and shift and not alt and key in {"o", "p"}:
            _terminal.feed_child(b"\x0f" if key == "o" else b"\x10")
            return True
        if control and not shift and not alt and key in {"o", "p"}:
            if self.has_foreground_program(_terminal):
                return False
            self.window.mode_shortcut(key)
            return True
        if control and not shift and not alt and key in {"1", "2", "3", "4"}:
            if self.has_foreground_program(_terminal):
                return False
            self.window.set_mode({"1": "files", "2": "project", "3": "console", "4": "gallery"}[key])
            return True
        return False

    def has_foreground_program(self, terminal):
        record = next((record for record in self.terminals if record[0] is terminal), None)
        if record is None or not record[1]:
            return False
        try:
            pty = terminal.get_pty()
            return pty is not None and os.tcgetpgrp(pty.get_fd()) != os.getpgid(record[1])
        except OSError:
            return False

    def new(self, command=None, reveal=True):
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
        fonts = self.window.appearance["fonts"]
        terminal_font = Pango.FontDescription()
        terminal_font.set_family(fonts["family"])
        terminal_font.set_absolute_size(fonts["files_px"] * Pango.SCALE)
        terminal_font.set_weight(Pango.Weight(fonts["files_weight"]))
        terminal.set_font(terminal_font)

        def rgba(value):
            color = Gdk.RGBA()
            color.parse(value)
            return color

        terminal.set_colors(rgba(self.window.palette.get("terminal_foreground", self.window.palette["foreground"])),
                            rgba(self.window.palette.get("terminal_background", self.window.palette["background"])),
                            [rgba(color) for color in self.window.appearance["ansi"]])
        terminal.set_audible_bell(False)
        terminal.set_scroll_on_keystroke(True)
        connect_click(terminal, self.menu)
        connect_key(terminal, self.terminal_shortcut, capture=True)
        terminal.workspace_ready = False
        terminal.workspace_spawned = False
        terminal.workspace_ready_source = 0
        terminal.workspace_exited = False
        terminal.workspace_pending = []
        terminal.workspace_mode = "console"
        terminal.workspace_output_return_mode = "files"
        terminal.workspace_project_return_mode = "files"
        terminal.workspace_browser = (self.capture_browser_state()
                                      if self.current is None
                                      or self.browser_state_tab is self.current
                                      else dict(self.current.workspace_browser))
        if self.window.mode == "console" and self.current is not None:
            self.update_cwd(self.current)
            folder = (self.current.workspace_cwd
                      or terminal.workspace_browser["paths"][terminal.workspace_browser["active"]])
            paths = list(terminal.workspace_browser["paths"])
            paths[terminal.workspace_browser["active"]] = folder
            terminal.workspace_browser["paths"] = tuple(paths)
        else:
            folder = (self.window.active.path if self.window.active is not None
                      else self.window.initial_browser_state["paths"][0])
        terminal.workspace_cwd = folder
        argv = command or [os.environ.get("SHELL", "/bin/bash"), "-i"]
        self.sequence += 1
        terminal.workspace_tab_number = self.sequence
        tab_name = str(folder)
        title = Gtk.Label(label=tab_name)
        page = scrolled(terminal)
        insert_at = (self.current.workspace_page_index + 1
                     if self.current is not None else len(self.terminals))
        preceding_tab = (self.terminals[insert_at - 1][0].workspace_tab_group
                         if insert_at else None)
        index = self.notebook.insert_page(page, title, insert_at)
        page.show()
        title.show()
        terminal.workspace_page_index = index
        tab_group = Box(spacing=0)
        tab_button = button(tab_name, lambda target=terminal: self.select(target.workspace_page_index))
        tab_button.add_css_class("top-tab")
        tab_button.set_tooltip_text(f"{tab_name} · {folder}")
        tab_button.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        tab_button.get_child().set_max_width_chars(24)
        close_button = button("×", lambda target=terminal: self.close(target))
        close_button.add_css_class("tab-close")
        close_button.set_tooltip_text(f"Close shell in {folder}")
        tab_group.pack_start(tab_button, False, False, 0)
        tab_group.pack_start(close_button, False, False, 0)
        self.tab_bar.insert_child_after(tab_group, preceding_tab)
        tab_group.show()
        terminal.workspace_tab_button = tab_button
        terminal.workspace_tab_group = tab_group
        record = [terminal, 0]
        self.terminals.insert(index, record)
        for number, (remaining, _pid) in enumerate(self.terminals):
            remaining.workspace_page_index = number
        if self.current is None:
            self.current = terminal

        def exited(*_):
            record[1] = 0
            terminal.workspace_exited = True
            terminal.workspace_ready = False
            if terminal.workspace_ready_source:
                GLib.source_remove(terminal.workspace_ready_source)
                terminal.workspace_ready_source = 0
            if not getattr(terminal, "workspace_closed", False):
                GLib.idle_add(lambda: self.close(terminal, force=True, from_shell_exit=True))

        terminal.connect("child-exited", exited)

        def ready():
            terminal.workspace_ready_source = 0
            if not terminal.workspace_spawned or terminal.workspace_exited or terminal.workspace_ready:
                return False
            terminal.workspace_ready = True
            self.flush_commands(terminal)
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
                self.window.failure(error)
            else:
                terminal.workspace_spawned = True
                GLib.timeout_add(600, lambda: False if terminal.workspace_ready_source else ready())
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(folder), argv, None,
            GLib.SpawnFlags.SEARCH_PATH, None, None, -1, None, spawned, None)
        if reveal:
            self.select(index)
        else:
            self.window.refresh_mode_buttons()
        return terminal

    def close(self, terminal, force=False, from_shell_exit=False):
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
                self.close(terminal, force=True)

            confirm(self.window, "Close this terminal tab and end its process?", None, approved)
            return
        terminal.workspace_closed = True
        was_active = terminal is self.current
        if terminal is self.window.command_terminal:
            self.window.command_terminal = None
        self.notebook.remove_page(index)
        self.tab_bar.remove(terminal.workspace_tab_group)
        self.terminals.pop(index)
        for number, (remaining, _pid) in enumerate(self.terminals):
            remaining.workspace_page_index = number
        if was_active:
            self.current = None
        if not self.terminals:
            if from_shell_exit:
                self.window.quit(on_cancel=self.new)
                return
            self.new()
        elif was_active:
            self.select(min(index, len(self.terminals) - 1))
        self.window.refresh_mode_buttons()

    def menu(self, terminal, event):
        if event.button == 1 and event.state & Gdk.ModifierType.CONTROL_MASK:
            return self.open_link(terminal, event)
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
                                ("Paste", lambda: self.paste_clipboard(terminal)),
                                ("Send Ctrl+O", lambda: terminal.feed_child(b"\x0f"))):
            content.append(button(title, lambda action=callback: (action(), menu.popdown())))
        menu.set_child(content)
        terminal.workspace_menu = menu
        menu.popup()
        return True

    def paste_clipboard(self, terminal):
        if terminal.workspace_exited or getattr(terminal, "workspace_closed", False):
            return
        formats = Gdk.Display.get_default().get_clipboard().get_formats()
        has_image = (formats.contain_gtype(Gdk.Texture.__gtype__)
                     or any(mime.startswith("image/") for mime in formats.get_mime_types() or ()))
        if has_image and self.has_foreground_program(terminal):
            # The foreground TUI (for example Codex) reads the image itself.
            terminal.feed_child(b"\x16")
        else:
            terminal.paste_clipboard()

    def open_link(self, terminal, event):
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
            self.window.failure(error)
        return True
