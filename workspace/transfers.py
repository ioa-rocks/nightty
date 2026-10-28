# SPDX-License-Identifier: MPL-2.0
"""Copy, move, delete and create-directory dialogs, background jobs and the ⇄ status button."""

from datetime import datetime
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gdk, GLib, Gtk, Pango

from .model import Deletion, Transfer
from .previews import format_size
from .widgets import Box, button, connect_key, dialog_button, label, scrolled


class FileOperations:
    """Owns the job list and worker pool; dialogs are MC-style modal windows over the main window."""

    def __init__(self, window):
        self.window = window
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="surface-copy")
        self.jobs = []
        self.alert = False
        self.popover = None
        self.jobs_label = None
        self.cancel_button = None
        self.button = button("⇄", self.show_jobs)
        self.button.set_tooltip_text("File operations")

    def running(self):
        return [job for job in self.jobs
                if job.status in {"queued", "copying", "moving", "conflict", "deleting"}]

    def shutdown(self):
        for job in self.jobs:
            job.cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def create_directory(self):
        pane = self.window.active
        folder = pane.path
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_title("Create directory")
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(580, -1)
        dialog.set_child(content)
        content.append(label("CREATE DIRECTORY", "mc-dialog-title"))
        fields = Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fields.add_css_class("mc-transfer-fields")
        content.append(fields)
        fields.append(label("In", "mc-transfer-caption"))
        location = label(str(folder), "mc-transfer-value")
        location.set_tooltip_text(str(folder))
        fields.append(location)
        fields.append(label("Directory name", "mc-transfer-caption"))
        name_entry = Gtk.Entry()
        name_entry.add_css_class("mc-transfer-entry")
        fields.append(name_entry)
        error = label("", "mc-transfer-error")
        error.set_wrap(True)
        error.set_ellipsize(Pango.EllipsizeMode.NONE)
        error.set_visible(False)
        content.append(error)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        create = Gtk.Button(label="_Create", use_underline=True)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        for choice in (create, cancel):
            dialog_button(choice)
            actions.append(choice)
        dialog.set_default_widget(create)
        creating = False

        def begin():
            nonlocal creating
            if creating:
                return
            name = name_entry.get_text()
            if not name.strip() or name in {".", ".."} or Path(name).name != name:
                error.set_text("Enter a directory name without path separators.")
                error.set_visible(True)
                return
            creating = True
            name_entry.set_sensitive(False)
            create.set_sensitive(False)
            create.set_label("Creating…")
            cancel.set_sensitive(False)
            error.set_visible(False)

            def created(_result):
                if not dialog.get_visible():
                    return
                dialog.destroy()
                if pane.path == folder:
                    pane.navigate(folder, selected_name=name)
                    (pane.gallery.flow if self.window.mode == "gallery" else pane.view).grab_focus()

            def failed(exc):
                nonlocal creating
                if not dialog.get_visible():
                    return
                creating = False
                name_entry.set_sensitive(True)
                create.set_sensitive(True)
                create.set_label("_Create")
                cancel.set_sensitive(True)
                error.set_text(str(exc))
                error.set_visible(True)
                name_entry.grab_focus()

            self.window.async_call(lambda: (folder / name).mkdir(), created, failed)

        create.connect("clicked", lambda *_: begin())
        cancel.connect("clicked", lambda *_: dialog.destroy())
        name_entry.connect("activate", lambda *_: begin())
        dialog.connect("close-request", lambda *_: creating)

        def dialog_key(_widget, event):
            if event.keyval == Gdk.KEY_Escape:
                if not creating:
                    dialog.destroy()
                return True
            return False

        connect_key(dialog, dialog_key, capture=True)
        dialog.present()
        name_entry.grab_focus()
        return dialog

    def delete(self):
        pane = self.window.active
        sources = self.window.active.gallery.selected_paths() if self.window.mode == "gallery" else pane.operation_paths()
        sources = [Path(source) for source in sources]
        if not sources:
            self.window.status.set_text("Select a file or mark files to delete (Insert or Ctrl+T).")
            return
        source_names = {str(source) for source in sources}
        positions = [index for index, row in enumerate(pane.entries) if row[3] in source_names]
        after = next((row[1] for row in pane.entries[max(positions, default=-1) + 1:]
                      if row[3] not in source_names), None)
        before = next((row[1] for row in reversed(pane.entries[:min(positions, default=0)])
                       if row[3] not in source_names), None)
        restore_name = after or before
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_title("Delete")
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        dialog.add_css_class("mc-danger-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(640, -1)
        dialog.set_child(content)
        content.append(label("DELETE PERMANENTLY", "mc-dialog-title"))
        source_text = str(sources[0]) if len(sources) == 1 else f"{sources[0]} and {len(sources) - 1} more"
        message = label(source_text, "mc-dialog-message")
        message.set_tooltip_text("\n".join(map(str, sources)))
        content.append(message)
        detail = label("Selected directories and all their contents will be removed. This cannot be undone.",
                       "mc-dialog-detail")
        detail.set_wrap(True)
        detail.set_ellipsize(Pango.EllipsizeMode.NONE)
        content.append(detail)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        delete = Gtk.Button(label="_Delete", use_underline=True)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        for choice in (delete, cancel):
            dialog_button(choice)
            actions.append(choice)
        delete.add_css_class("mc-danger-default")
        dialog.set_default_widget(delete)

        def begin():
            dialog.destroy()
            job = Deletion(sources)
            job.source_pane_index = pane.index
            job.restore_name = restore_name
            self.jobs.append(job)
            self.pool.submit(job.run)
            self.show_delete_progress(job)

        delete.connect("clicked", lambda *_: begin())
        cancel.connect("clicked", lambda *_: dialog.destroy())
        connect_key(dialog, lambda _widget, event: (dialog.destroy(), True)[1]
                    if event.keyval == Gdk.KEY_Escape else False, capture=True)
        dialog.present()
        delete.grab_focus()
        return dialog

    def show_delete_progress(self, job):
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        dialog.add_css_class("mc-danger-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(600, -1)
        dialog.set_child(content)
        content.append(label("DELETING", "mc-dialog-title"))
        message = label("Starting…", "mc-dialog-message")
        content.append(message)
        progress = Gtk.ProgressBar()
        progress.add_css_class("mc-transfer-progress")
        content.append(progress)
        detail = label(str(job.sources[0]), "mc-dialog-detail")
        detail.set_wrap(True)
        detail.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        content.append(detail)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        abort = Gtk.Button(label="_Abort", use_underline=True)
        dialog_button(abort)
        abort.add_css_class("mc-danger-default")
        actions.append(abort)
        dialog.set_default_widget(abort)

        def stop():
            job.cancel.set()
            abort.set_sensitive(False)
            message.set_text("Stopping after the current item…")

        def restore_focus():
            pane = self.window.panes[job.source_pane_index]
            if pane is self.window.active and self.window.mode in {"files", "gallery"}:
                (pane.gallery.flow if self.window.mode == "gallery" else pane.view).grab_focus()
            return False

        def finish():
            dialog.destroy()
            GLib.idle_add(restore_focus)

        def close_request(_dialog):
            if job.status in {"queued", "deleting"}:
                stop()
                return True
            GLib.idle_add(restore_focus)
            return False

        def refresh():
            if not dialog.get_visible():
                return False
            if job.status in {"queued", "deleting"}:
                if not job.cancel.is_set():
                    message.set_text(f"{job.files} items removed · {format_size(job.bytes)}")
                if job.current:
                    detail.set_text(job.current)
                progress.pulse()
                return True
            message.set_text(f"Delete {job.status}: {job.files} items removed")
            detail.set_text(job.error or ("Some items were already removed." if job.status == "cancelled"
                                          else "Finished."))
            progress.set_fraction(1 if job.status == "completed" else 0)
            abort.set_label("_Close")
            abort.set_sensitive(True)
            return False

        abort.connect("clicked", lambda *_: stop() if job.status in {"queued", "deleting"}
                      else finish())
        dialog.connect("close-request", close_request)
        def dialog_key(_widget, event):
            if event.keyval == Gdk.KEY_Escape:
                if job.status in {"queued", "deleting"}:
                    stop()
                else:
                    finish()
                return True
            return False

        connect_key(dialog, dialog_key, capture=True)
        dialog.present()
        abort.grab_focus()
        refresh()
        GLib.timeout_add(120, refresh)
        return dialog

    def copy(self, same_folder=False):
        return self.transfer_dialog("copy", same_folder)

    def move(self, same_folder=False):
        return self.transfer_dialog("move", same_folder)

    def transfer_dialog(self, operation, same_folder=False):
        sources = self.window.active.gallery.selected_paths() if self.window.mode == "gallery" else (
            [self.window.active.current_path()] if same_folder else self.window.active.operation_paths())
        sources = [Path(source) for source in sources if source is not None]
        if not sources:
            self.window.status.set_text(f"Select a file or mark files to {operation} (Insert or Ctrl+T).")
            return
        suggested = sources[0] if same_folder else self.window.panes[1 - self.window.active.index].path
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_title("Copy" if operation == "copy" else "Move / Rename")
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(680, -1)
        dialog.set_child(content)
        title = label("COPY FILES" if operation == "copy" else "MOVE / RENAME", "mc-dialog-title")
        content.append(title)
        fields = Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fields.add_css_class("mc-transfer-fields")
        content.append(fields)
        source_title = label("From", "mc-transfer-caption")
        fields.append(source_title)
        source_text = (str(sources[0]) if len(sources) == 1 else
                       f"{sources[0]} and {len(sources) - 1} more")
        source_value = label(source_text, "mc-transfer-value")
        source_value.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        source_value.set_tooltip_text("\n".join(map(str, sources)))
        fields.append(source_value)
        fields.append(label("To", "mc-transfer-caption"))
        destination_entry = Gtk.Entry()
        destination_entry.add_css_class("mc-transfer-entry")
        destination_entry.set_text(str(suggested))
        destination_entry.set_hexpand(True)
        fields.append(destination_entry)
        hint = label("Existing files will ask before replacement.", "mc-dialog-detail")
        content.append(hint)
        error = label("", "mc-transfer-error")
        error.set_wrap(True)
        error.set_ellipsize(Pango.EllipsizeMode.NONE)
        error.set_visible(False)
        content.append(error)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        yes = Gtk.Button(label="_Yes", use_underline=True)
        background = Gtk.Button(label="_Background", use_underline=True)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        for choice in (yes, background, cancel):
            dialog_button(choice)
            actions.append(choice)
        dialog.set_default_widget(yes)
        checking = False

        def begin(in_background):
            nonlocal checking
            if checking:
                return
            raw = destination_entry.get_text().strip()
            if not raw:
                error.set_text("Enter a destination.")
                error.set_visible(True)
                return
            checking = True
            destination_entry.set_sensitive(False)
            yes.set_sensitive(False)
            background.set_sensitive(False)
            error.set_visible(False)

            def resolve_destination():
                target = Path(raw).expanduser()
                if not target.is_absolute():
                    target = sources[0].parent / target
                if target.is_dir():
                    destination, target_name = target, None
                elif len(sources) == 1 and not raw.endswith("/"):
                    destination, target_name = target.parent, target.name
                    if not destination.is_dir():
                        raise ValueError(f"Destination folder does not exist: {destination}")
                else:
                    raise ValueError("Select an existing destination folder for multiple files.")
                return destination, target_name

            def failed(exc):
                nonlocal checking
                if not dialog.get_visible():
                    return
                checking = False
                destination_entry.set_sensitive(True)
                yes.set_sensitive(True)
                background.set_sensitive(True)
                error.set_text(str(exc))
                error.set_visible(True)

            def ready(resolved):
                if not dialog.get_visible():
                    return
                destination, target_name = resolved
                dialog.destroy()
                job = self.start_transfer(sources, destination, target_name, operation,
                                          background=in_background)
                if not in_background:
                    self.show_transfer_progress(job)

            self.window.async_call(resolve_destination, ready, failed)

        yes.connect("clicked", lambda *_: begin(False))
        background.connect("clicked", lambda *_: begin(True))
        cancel.connect("clicked", lambda *_: dialog.destroy())
        destination_entry.connect("activate", lambda *_: yes.emit("clicked"))
        connect_key(dialog, lambda _widget, event: (dialog.destroy(), True)[1]
                    if event.keyval == Gdk.KEY_Escape else False, capture=True)
        dialog.present()
        if same_folder:
            destination_entry.grab_focus()
            destination_entry.select_region(len(str(sources[0].parent)) + 1, -1)
        else:
            yes.grab_focus()
        return dialog

    def show_transfer_progress(self, job):
        dialog = Gtk.Window(transient_for=self.window, modal=True)
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(580, -1)
        dialog.set_child(content)
        content.append(label("COPYING" if job.operation == "copy" else "MOVING", "mc-dialog-title"))
        message = label("Starting…", "mc-dialog-message")
        content.append(message)
        progress = Gtk.ProgressBar()
        progress.add_css_class("mc-transfer-progress")
        content.append(progress)
        detail = label(str(job.destination / (job.target_name or job.sources[0].name)), "mc-dialog-detail")
        detail.set_wrap(True)
        detail.set_ellipsize(Pango.EllipsizeMode.NONE)
        content.append(detail)
        actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        actions.add_css_class("mc-dialog-actions")
        content.append(actions)
        switch = Gtk.Button(label="Switch to _Background", use_underline=True)
        cancel = Gtk.Button(label="_Cancel", use_underline=True)
        for choice in (switch, cancel):
            dialog_button(choice)
            actions.append(choice)
        dialog.set_default_widget(switch)

        def close():
            if job.status in {"queued", "copying", "moving", "conflict"}:
                job.background = True
            dialog.destroy()

        def cancel_progress():
            job.cancel.set()
            cancel.set_sensitive(False)
            message.set_text("Cancelling…")

        def closing(_dialog):
            if job.status in {"queued", "copying", "moving", "conflict"}:
                job.background = True
            return False

        switch.connect("clicked", lambda *_: close())
        cancel.connect("clicked", lambda *_: cancel_progress())
        dialog.connect("close-request", closing)
        connect_key(dialog, lambda _widget, event: (close(), True)[1]
                    if event.keyval == Gdk.KEY_Escape else False, capture=True)

        def refresh():
            if not dialog.get_visible():
                return False
            if job.status == "conflict":
                message.set_text("Waiting for an overwrite choice…")
                self.show_conflict_dialog(job, parent=dialog)
                return True
            if job.status in {"queued", "copying", "moving"}:
                message.set_text(f"{job.files} files · {format_size(job.bytes)}")
                progress.pulse()
                return True
            if job.status == "completed":
                dialog.destroy()
                return False
            message.set_text(f"{job.operation.title()} {job.status}: {job.files} files · {format_size(job.bytes)}")
            if job.error:
                detail.set_text(job.error)
            progress.set_fraction(0)
            switch.set_label("_Close")
            cancel.set_visible(False)
            return False

        dialog.present()
        refresh()
        GLib.timeout_add(120, refresh)
        return dialog

    def show_conflict_dialog(self, job, parent=None):
        conflict = job.conflict
        if conflict is None:
            return None
        previous = getattr(job, "conflict_dialog", None)
        if (previous is not None and previous.get_visible()
                and getattr(previous, "nightty_conflict", None) is conflict):
            return previous
        dialog = Gtk.Window(transient_for=parent or self.window, modal=True)
        dialog.set_decorated(False)
        dialog.set_resizable(False)
        dialog.add_css_class("mc-dialog")
        dialog.nightty_conflict = conflict
        job.conflict_dialog = dialog
        content = Box(orientation=Gtk.Orientation.VERTICAL)
        content.add_css_class("mc-dialog-content")
        content.set_size_request(720, -1)
        dialog.set_child(content)
        content.append(label("FILE EXISTS", "mc-dialog-title"))
        fields = Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fields.add_css_class("mc-transfer-fields")
        content.append(fields)
        for caption, path, info in (("Source", conflict.source, conflict.source_stat),
                                    ("Destination", conflict.target, conflict.target_stat)):
            modified = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M")
            fields.append(label(f"{caption} · {format_size(info.st_size)} · {modified}",
                                "mc-transfer-caption"))
            value = label(str(path), "mc-transfer-value")
            value.set_tooltip_text(str(path))
            fields.append(value)
        buttons = (("_Yes", "yes"), ("_No", "no"), ("_Abort", "abort"),
                   ("_All", "all"), ("N_one", "none"), ("Size _differs", "size_differs"),
                   ("If _older", "older"), ("If _smaller", "smaller"))
        first = None
        for offset in (0, 3, 6):
            actions = Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            actions.add_css_class("mc-dialog-actions" if offset == 0 else "mc-conflict-actions")
            content.append(actions)
            for text, decision in buttons[offset:offset + 3]:
                choice = Gtk.Button(label=text, use_underline=True)
                dialog_button(choice)
                actions.append(choice)
                if first is None:
                    first = choice
                choice.connect("clicked", lambda _button, value=decision: answer(value))

        def answer(decision):
            job.answer_conflict(conflict, decision)
            if getattr(job, "conflict_dialog", None) is dialog:
                job.conflict_dialog = None
            dialog.destroy()

        def closing(_dialog):
            if not conflict.ready.is_set():
                job.answer_conflict(conflict, "abort")
            if getattr(job, "conflict_dialog", None) is dialog:
                job.conflict_dialog = None
            return False

        dialog.connect("close-request", closing)
        connect_key(dialog, lambda _widget, event: (answer("abort"), True)[1]
                    if event.keyval == Gdk.KEY_Escape else False, capture=True)
        dialog.set_default_widget(first)
        dialog.present()
        first.grab_focus()
        return dialog

    def start_copy(self, sources, destination):
        return self.start_transfer(sources, destination)

    def start_transfer(self, sources, destination, target_name=None, operation="copy", background=True):
        job = Transfer(sources, destination, target_name=target_name, operation=operation,
                       interactive=True)
        job.background = background
        self.jobs.append(job)
        self.pool.submit(job.run)
        self.window.status.set_text(f"{operation.title()} queued; you can keep navigating.")
        if self.window.mode == "files":
            def restore_footer():
                if self.window.mode == "files":
                    self.window.refresh_file_footer(self.window.active)
                return False

            GLib.timeout_add_seconds(2, restore_footer)
        return job

    def update(self):
        running = [job for job in self.jobs if job.status in {"queued", "copying", "moving", "conflict", "deleting"}]
        waiting = [job for job in running if job.status == "conflict" and job.conflict is not None]
        self.button.set_label(f"⇄ {len(running)}" if running else "⇄")
        for job in self.jobs:
            conflict = getattr(job, "conflict", None)
            if conflict is not None and getattr(job, "background", False) and not conflict.alerted:
                conflict.alerted = True
                self.alert = True
                display = Gdk.Display.get_default()
                if display is not None:
                    display.beep()
                    GLib.timeout_add(180, lambda: (display.beep(), False)[1])
            if job.status in {"completed", "failed", "cancelled"} and not getattr(job, "announced", False):
                job.announced = True
                if job.status == "failed" and getattr(job, "background", True):
                    self.alert = True
                    display = Gdk.Display.get_default()
                    if display is not None:
                        display.beep()
                        GLib.timeout_add(180, lambda: (display.beep(), False)[1])
                for pane in self.window.panes:
                    if pane.results_active:
                        changed_tree = (any(Path(source).is_relative_to(pane.path)
                                            for source in job.sources)
                                        or (job.destination is not None and
                                            Path(job.destination).is_relative_to(pane.path)))
                        if pane.results_scope == "indexed" or changed_tree:
                            self.window.repeat_find(pane)
                        continue
                    refresh_paths = [job.destination] if job.destination is not None else []
                    if job.operation in {"move", "delete"}:
                        refresh_paths.extend(Path(source).parent for source in job.sources)
                    if any(os.path.abspath(os.fspath(pane.path)) == os.path.abspath(os.fspath(path))
                           for path in refresh_paths):
                        selected_name = (getattr(job, "restore_name", None)
                                         if job.operation == "delete" and pane.index == job.source_pane_index
                                         else None)
                        pane.navigate(pane.path, selected_name=selected_name)
                if self.window.mode != "files":
                    unit = "items" if job.operation == "delete" else "files"
                    self.window.status.set_text(f"{job.operation.title()} {job.status}: {job.files} {unit}, {format_size(job.bytes)}" + (f" · {job.error}" if job.error else ""))
        if self.alert or waiting:
            self.button.set_label(f"⇄ !{len(running) or ''}")
            self.button.add_css_class("transfer-alert")
        else:
            self.button.remove_css_class("transfer-alert")
        if running:
            progress = f"{len(running)} operation(s) · " + " · ".join(
                f"{job.files} {'items' if job.operation == 'delete' else 'files'} / {format_size(job.bytes)}" for job in running)
            self.button.set_tooltip_text(
                "File exists — choose an overwrite action" if waiting else progress)
            if self.window.mode != "files":
                self.window.status.set_text(progress)
        else:
            self.button.set_tooltip_text("Transfer failed — open for details" if self.alert else "File operations")
        if self.jobs_label is not None and self.jobs_label.get_parent():
            descriptions = []
            for job in self.jobs:
                unit = "items" if job.operation == "delete" else "files"
                target = (job.sources[0] if job.operation == "delete" else
                          job.destination / (job.target_name or job.sources[0].name))
                skipped = f" · {job.skipped} skipped" if getattr(job, "skipped", 0) else ""
                descriptions.append(f"{job.operation.upper()} {job.status.upper()}   "
                                    f"{job.files} {unit} · {format_size(job.bytes)}{skipped}\n{target}\n{job.error}")
            self.jobs_label.set_text("\n\n".join(descriptions) or "No operations yet.")
            self.cancel_button.set_sensitive(bool(running))
        return True

    def show_jobs(self):
        waiting = next((job for job in self.jobs
                        if job.status == "conflict" and job.conflict is not None), None)
        if waiting is not None:
            if self.popover is not None and self.popover.get_visible():
                self.popover.popdown()
            self.alert = False
            self.show_conflict_dialog(waiting)
            return
        if self.popover is None:
            self.popover = Gtk.Popover()
            self.popover.set_parent(self.button)
            content = Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
            content.pack_start(label("FILE OPERATIONS", "section"), False, False, 0)
            self.jobs_label = label("")
            self.jobs_label.set_ellipsize(Pango.EllipsizeMode.NONE)
            self.jobs_label.set_wrap(True)
            jobs_scroll = scrolled(self.jobs_label)
            jobs_scroll.set_size_request(360, 180)
            content.pack_start(jobs_scroll, True, True, 0)
            self.cancel_button = button("Cancel running operations", lambda:
                [job.cancel.set() for job in self.jobs if job.status in {"queued", "copying", "moving", "conflict", "deleting"}])
            content.pack_start(self.cancel_button, False, False, 0)
            self.popover.set_child(content)
        if self.popover.get_visible():
            self.popover.popdown()
            return
        self.update()
        self.alert = False
        self.update()
        self.popover.show()
        self.popover.popup()
