"""Launch a native Workspace; repeated invocations toggle the existing window."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .app import Gio, GLib, Gtk, Window
from .model import State


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("directory", nargs="?", help="open this directory in the left pane")
    result.add_argument("--left")
    result.add_argument("--right")
    result.add_argument("--state", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "surface-workspace")
    result.add_argument("--backend", default="http://127.0.0.1:8098")
    result.add_argument("--smoke", action="store_true", help="exercise disposable native fixtures and save screenshots")
    return result


class Application(Gtk.Application):
    def __init__(self, args):
        flags = Gio.ApplicationFlags.NON_UNIQUE if args.smoke else Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        super().__init__(application_id="pw.blackbird.SurfaceWorkspace", flags=flags)
        self.window, self.args, self.result = None, args, 0
        self.client = None

    def do_activate(self):
        if self.window is None:
            state = State(self.args.state)
            self.window = Window(self, self.args, state, client=self.client)
        elif self.window.get_visible():
            self.window.hide()
        else:
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
        if self.window is None:
            self.args = args
        elif args.left or args.right:
            for pane, folder in zip(self.window.panes, (args.left, args.right)):
                if folder:
                    pane.navigate(Path(folder))
            self.window.show()
            self.window.present()
            return 0
        self.activate()
        return 0


def smoke(args):
    """Runs the real widgets with local fixtures, fake agents and one fixture shell."""
    import cairo
    from .app import Vte

    class FakeAgents:
        calls = []
        interrupt_next = True

        def snapshot(self):
            return {"profiles": [{"id": "fixture", "name": "Research helper", "session_mode": "continuous", "execution": "Fixture", "collection": "public-fixture"}], "sessions": []}

        def create(self, profile):
            self.calls.append(("create", profile))
            return {"id": "fixture-thread"}

        def send(self, profile, session, key, text):
            self.calls.append(("send", profile, session, key, text))
            if self.interrupt_next:
                self.interrupt_next = False
                raise RuntimeError("Fixture interrupted delivery")
            return {"id": session, "status": "completed"}

        def detail(self, profile, session):
            return {"id": session, "status": "completed", "messages": [
                {"role": "assistant", "content": "Fixture response <b>kept as text</b>"}]}

    with tempfile.TemporaryDirectory(prefix="workspace-native-fixture-") as directory:
        root = Path(directory)
        project, output = root / "Publishing", root / "Outbox"
        child = project / "A language article"
        child.mkdir(parents=True)
        output.mkdir()
        (project / ".surface.json").write_text(json.dumps({"type": "project", "title": "Publishing", "description": "Field notes, technical writing and language.", "agent": "fixture"}))
        (child / ".surface.json").write_text(json.dumps({"type": "project", "title": "A language article", "description": "Explain an interesting phrase with evidence.", "work": ["check-the-claim.md"]}))
        (child / "README.md").write_text("# A language article\n\nA working draft with sources and one outstanding claim.\n\n## Next action\nCheck the evidence before revising the opening.\n")
        (child / "draft.md").write_text("# A word worth noticing\n\nA draft opened directly from its project folder.\n")
        (child / "check-the-claim.md").write_text("# Check the claim\n\nWaiting for a reliable dictionary citation.\n")
        image = cairo.ImageSurface(cairo.FORMAT_ARGB32, 900, 480)
        drawing = cairo.Context(image)
        drawing.set_source_rgb(.12, .20, .29)
        drawing.paint()
        for x, y, radius, color in [(180, 235, 90, (.45, .72, .69)), (450, 210, 110, (.82, .65, .40)), (720, 260, 70, (.55, .65, .86))]:
            drawing.set_source_rgb(*color)
            drawing.arc(x, y, radius, 0, 6.284)
            drawing.fill()
        image.write_to_png(str(child / "visual.png"))
        args.left, args.right = str(child), str(output)
        args.state = root / "state"
        app = Application(args)
        fake = FakeAgents()
        app.client = fake
        checks = []
        step = {"number": 0, "ticks": 0}

        def screenshot(name):
            window = app.window
            window.check_resize()
            width, height = window.get_allocated_width(), window.get_allocated_height()
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
            window.draw(cairo.Context(surface))
            surface.write_to_png("/tmp/workspace-" + name + ".png")

        def run():
            try:
                step["ticks"] += 1
                if step["ticks"] > 180:
                    raise AssertionError(f"Native smoke timed out at step {step['number']}")
                window = app.window
                if window is None:
                    app.window = Window(app, args, State(args.state), client=fake)
                    window = app.window
                window.stack.set_transition_type(Gtk.StackTransitionType.NONE)
                if not window.context or len(window.panes[0].store) < 5:
                    return True
                number = step["number"]
                if number == 0:
                    assert len(window.context["projects"]) == 2
                    assert window.context["agent"] == "fixture"
                    window.add_favourite()
                    assert child.resolve() in window.state.favourites
                    checks.extend(["nested project context", "inherited agent suggestion", "favourite persistence"])
                    screenshot("files")
                    window.job = window.start_copy([child / "draft.md"], output)
                    window.preview(child / "visual.png")
                    step["number"] = 1
                elif number == 1:
                    if window.job.status != "completed" or window.preview_stack.get_visible_child_name() != "image":
                        return True
                    assert (output / "draft.md").read_text() == (child / "draft.md").read_text()
                    assert window.panes[1].path == output
                    checks.extend(["background copy", "image preview", "pane positions preserved"])
                    screenshot("project")
                    window.new_terminal(command=["/bin/sh", "-c", "printf 'SURFACE_TERMINAL_OK\\n'; exec /bin/sh"])
                    window.set_mode("console")
                    step["number"] = 2
                elif number == 2:
                    terminal = window.terminals[-1][0]
                    text = terminal.get_text_format(Vte.Format.TEXT) or ""
                    if "SURFACE_TERMINAL_OK" not in [line.strip() for line in text.splitlines()]:
                        return True
                    window.hide()
                    assert any(pid for _terminal, pid in window.terminals)
                    window.show()
                    checks.extend(["embedded terminal output", "hide preserves process"])
                    screenshot("console")
                    window.set_mode("project")
                    window.project_tabs.set_current_page(3)
                    step["number"] = 3
                elif number == 3:
                    if window.profile_picker.get_active_id() != "fixture":
                        return True
                    assert not fake.calls
                    window.compose.get_buffer().set_text("Inspect this fixture")
                    window.send_agent()
                    step["number"] = 4
                elif number == 4:
                    if window.agent_sending:
                        return True
                    # Refresh can replace the composer buffer; the retry must
                    # still keep its previously created session and request ID.
                    window.refresh_agents()
                    step["number"] = 5
                elif number == 5:
                    if window.agent_loading or window.profile_picker.get_active_id() != "fixture":
                        return True
                    window.send_agent()
                    step["number"] = 6
                elif number == 6:
                    text = window.chat.get_buffer().get_text(window.chat.get_buffer().get_start_iter(), window.chat.get_buffer().get_end_iter(), False)
                    if "Fixture response" not in text:
                        return True
                    assert "<b>kept as text</b>" in text
                    creates = [call for call in fake.calls if call[0] == "create"]
                    sends = [call for call in fake.calls if call[0] == "send"]
                    assert len(creates) == 1 and len(sends) == 2 and sends[0] == sends[1]
                    checks.extend(["no inference on navigation", "explicit agent send", "native response rendering"])
                    checks.append("interrupted delivery retains session and request identity")
                    screenshot("agents")
                    print(json.dumps({"checks": checks, "screenshots": ["/tmp/workspace-" + mode + ".png" for mode in ("files", "project", "console", "agents")]}), flush=True)
                    app.quit()
                    return False
                return True
            except Exception:
                import traceback
                traceback.print_exc()
                app.result = 1
                app.quit()
                return False

        GLib.timeout_add(150, run)
        app.run([sys.argv[0]])
        return app.result


def main():
    args = parser().parse_args()
    args.left = args.left or args.directory
    if args.smoke:
        return smoke(args)
    app = Application(args)
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
