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
    launcher = Path.home() / ".local/bin/workspace"
    expected = "#!/bin/sh\nexec " + shlex.quote(str(source / "run.sh")) + ' "$@"\n'
    if launcher.exists() and launcher.read_text() != expected:
        raise SystemExit(f"Existing launcher left unchanged: {launcher}")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(expected)
    launcher.chmod(0o755)
    desktop = Path.home() / ".local/share/applications/pw.blackbird.SurfaceWorkspace.desktop"
    if desktop.exists() and "X-Surface-Workspace=true" not in desktop.read_text():
        raise SystemExit("An unrelated desktop entry already exists")
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Workspace\n"
        "Comment=Files, projects and embedded terminals\nIcon=system-file-manager\n"
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
    for field, value in (("name", "Workspace"), ("command", str(launcher)), ("binding", args.shortcut)):
        settings("set", binding_schema + ":" + path, field, value)
    if path not in entries:
        settings("set", schema, key, repr([*entries, path]))
    print(f"Show/hide shortcut installed: {args.shortcut}")


if __name__ == "__main__":
    main()
