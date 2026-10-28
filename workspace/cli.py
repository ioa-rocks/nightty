# SPDX-License-Identifier: MPL-2.0
"""Command line and GtkApplication glue: launch or present Nightty; --toggle controls the shortcut."""

import argparse
import os
from pathlib import Path
import sys

from .app import Gio, Gtk, Window
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
    return result


class Application(Gtk.Application):
    def __init__(self, args, *, unique=True):
        """A non-unique application runs its own window; tests use that mode."""
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE if unique else Gio.ApplicationFlags.NON_UNIQUE
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
            if args.maximized and not (args.toggle and self.window.get_visible()):
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


def main():
    args = parser().parse_args()
    args.left = args.left or args.directory
    app = Application(args)
    return app.run(sys.argv)
