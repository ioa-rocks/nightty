# SPDX-License-Identifier: MPL-2.0
"""Install user launchers and, optionally, one GNOME keyboard binding."""

import argparse
import ast
from pathlib import Path
import shlex
import subprocess


def settings(*args):
    return subprocess.check_output(["gsettings", *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortcut", help="GNOME accelerator, e.g. F12")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    launcher = Path.home() / ".local/bin/nightty"
    desktop = Path.home() / ".local/share/applications/pw.blackbird.SurfaceWorkspace.desktop"
    desktop_text = desktop.read_text() if desktop.exists() else ""
    owned_desktop = "X-Surface-Workspace=true" in desktop_text
    if desktop.exists() and not owned_desktop:
        raise SystemExit("An unrelated desktop entry already exists")
    command = shlex.quote(str(source / "run.sh"))
    previous = "#!/bin/sh\nexec " + command + ' "$@"\n'
    expected = "#!/bin/sh\nexec " + command + ' --toggle "$@"\n'
    allowed_launchers = {previous, expected}
    if owned_desktop:
        previous_sources = {source.with_name("Workspace")}
        for line in desktop_text.splitlines():
            if line.startswith("Icon="):
                icon = Path(line.removeprefix("Icon="))
                if icon.name in {"nightty-icon.png", "nightty-icon-constellation.png"}:
                    previous_sources.add(icon.parent.parent)
        for previous_source in previous_sources:
            old_command = shlex.quote(str(previous_source / "run.sh"))
            allowed_launchers.update({
                "#!/bin/sh\nexec " + old_command + ' "$@"\n',
                "#!/bin/sh\nexec " + old_command + ' --toggle "$@"\n',
            })
    if launcher.exists() and launcher.read_text() not in allowed_launchers:
        raise SystemExit(f"Existing launcher left unchanged: {launcher}")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(expected)
    launcher.chmod(0o755)
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Nightty\n"
        f"Comment=Files, projects and embedded terminals\nIcon={source / 'assets/nightty-icon-constellation.png'}\n"
        f"Exec={launcher}\nTerminal=false\nCategories=Utility;FileManager;\n"
        "StartupWMClass=pw.blackbird.SurfaceWorkspace\nX-Surface-Workspace=true\n")
    print(f"Launcher installed: {launcher}")
    if not args.shortcut:
        return
    schema = "org.gnome.settings-daemon.plugins.media-keys"
    key = "custom-keybindings"
    entries = ast.literal_eval(settings("get", schema, key).removeprefix("@as "))
    path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/surface-workspace/"
    binding_schema = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    for other in entries:
        if other != path and ast.literal_eval(settings("get", binding_schema + ":" + other, "binding")) == args.shortcut:
            print(f"{args.shortcut} is already assigned; existing shortcut left unchanged")
            return
    for field, value in (("name", "Nightty"), ("command", str(launcher)), ("binding", args.shortcut)):
        settings("set", binding_schema + ":" + path, field, value)
    if path not in entries:
        settings("set", schema, key, repr([*entries, path]))
    print(f"Show/hide shortcut installed: {args.shortcut}")


if __name__ == "__main__":
    main()
