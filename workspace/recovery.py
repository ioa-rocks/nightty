# SPDX-License-Identifier: MPL-2.0
"""Minimal terminal for recovering when the main Nightty window cannot start."""

import argparse
import os
from pathlib import Path
import sys

# Set this before importing GTK. Cairo avoids GPU buffer import failures.
os.environ["GSK_RENDERER"] = "cairo"

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gio, GLib, Gtk, Vte


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("directory", nargs="?")
    result.add_argument("--left")
    result.add_argument("--right")
    result.add_argument("--state")
    result.add_argument("--appearance")
    result.add_argument("--backend")
    result.add_argument("--maximized", action="store_true")
    result.add_argument("--toggle", action="store_true")
    result.add_argument("--safe-mode", action="store_true")
    return result


class RecoveryApplication(Gtk.Application):
    def __init__(self, args):
        # Stay separate even when a normal Nightty process owns its app ID.
        super().__init__(application_id="pw.blackbird.NighttyRecovery",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args = args
        self.window = None
        self.terminal = None

    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self, title="Nightty Recovery Terminal")
        self.window.set_default_size(1000, 700)
        if self.args.maximized:
            self.window.maximize()
        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(10000)
        self.window.set_child(self.terminal)
        self.window.present()

        folder = Path(self.args.left or self.args.directory or Path.cwd()).expanduser()
        if not folder.is_dir():
            folder = Path.home()

        def spawned(_terminal, _pid, error, _data):
            if error:
                self.window.set_child(Gtk.Label(label=f"Could not start /bin/sh: {error}"))

        self.terminal.connect("child-exited", lambda *_: self.quit())
        self.terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(folder), ["/bin/sh", "-i"], None,
                                  GLib.SpawnFlags.DEFAULT, None, None, -1, None, spawned, None)
        self.terminal.grab_focus()


def main():
    args = parser().parse_args()
    return RecoveryApplication(args).run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
