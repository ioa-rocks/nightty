# SPDX-License-Identifier: MPL-2.0
import json
import errno
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from workspace.agents import Agents
from workspace.appearance import load_appearance
from workspace.model import Deletion, State, Transfer, context_for, directory_entries, sort_directory_rows


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_with_conflict_choice(self, job, choice):
        worker = threading.Thread(target=job.run, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while job.conflict is None and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(job.conflict, (job.status, job.error))
            self.assertTrue(job.answer_conflict(job.conflict, choice))
            worker.join(3)
            self.assertFalse(worker.is_alive(), "transfer waited for another conflict")
            return job
        finally:
            job.cancel.set()
            worker.join(1)

    def test_inherited_context_keeps_reference_origin_and_nested_projects(self):
        parent = self.root / "startup"
        child = parent / "shares" / "documents"
        child.mkdir(parents=True)
        (parent / ".surface.json").write_text(json.dumps({"type": "project", "title": "Startup", "agent": "general", "work": ["tasks/parent.md"]}))
        (child.parent / "README.md").write_text("---\ntype: project\ntitle: Shares\nwork: [next.md]\nagent: legal\n---\n# Shares\n")
        context = context_for(child)
        self.assertEqual([p["title"] for p in context["projects"]], ["Startup", "Shares"])
        self.assertEqual(context["root"], child.parent)
        self.assertEqual(context["agent"], "legal")
        self.assertEqual(context["work"], [parent / "tasks/parent.md", child.parent / "next.md"])
        self.assertNotIn("command", context)

    def test_malformed_child_metadata_does_not_remove_parent_context(self):
        child = self.root / "child"
        child.mkdir()
        (self.root / ".surface.json").write_text('{"type":"project","title":"Parent"}')
        (child / ".surface.json").write_text("broken")
        context = context_for(child)
        self.assertEqual(context["title"], "Parent")
        self.assertTrue(context["warnings"])

    def test_favourites_and_pane_positions_persist_without_project_writes(self):
        state = State(self.root / "private")
        state.toggle_favourite(self.root)
        state.save(left=str(self.root), right="/tmp", mode="project")
        reopened = State(self.root / "private")
        self.assertEqual(reopened.favourites, [self.root])
        self.assertEqual(reopened.data["mode"], "project")
        reopened.toggle_favourite(self.root)
        self.assertEqual(State(self.root / "private").favourites, [])

    def test_recent_folders_keep_latest_unique_paths(self):
        state = State(self.root / "private")
        folders = [self.root / f"folder-{number}" for number in range(20)]
        for folder in folders:
            state.remember_folder(folder)
        state.remember_folder(folders[10])
        recent = State(self.root / "private").recent_folders
        self.assertEqual(recent[0], folders[10])
        self.assertEqual(len(recent), 16)
        self.assertEqual(len(set(recent)), 16)

    def test_appearance_file_rejects_invalid_colors(self):
        appearance = load_appearance()
        self.assertEqual(appearance["palette"]["background"], "#073642")
        appearance["palette"]["background"] = "red; background:black"
        path = self.root / "appearance.json"
        path.write_text(json.dumps(appearance))
        with self.assertRaisesRegex(ValueError, "#RRGGBB"):
            load_appearance(path)

    def test_copy_preserves_links_and_refuses_overwrite_and_self_copy(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        (source / "file.txt").write_text("source data")
        (source / "link").symlink_to("file.txt")
        job = Transfer([source], target).run()
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual((target / "source/file.txt").read_text(), "source data")
        self.assertTrue((target / "source/link").is_symlink())
        self.assertEqual(os.readlink(target / "source/link"), "file.txt")
        (source / "file.txt").write_text("new data")
        repeated = Transfer([source], target).run()
        self.assertEqual(repeated.status, "failed")
        self.assertEqual((target / "source/file.txt").read_text(), "source data")
        nested = source / "nested"
        nested.mkdir()
        self.assertEqual(Transfer([source], nested).run().status, "failed")

    def test_cancelled_copy_never_publishes_partial_file(self):
        source, target = self.root / "big.bin", self.root / "destination"
        source.write_bytes(b"x" * 4_000_000)
        target.mkdir()

        class CancelDuringRead:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 4

        job = Transfer([source], target, cancel=CancelDuringRead()).run()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse((target / source.name).exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_batch_preflight_does_not_copy_when_another_destination_exists(self):
        destination = self.root / "dest"
        destination.mkdir()
        a, b = self.root / "a", self.root / "b"
        a.write_text("a")
        b.write_text("b")
        (destination / "b").write_text("keep")
        job = Transfer([a, b], destination).run()
        self.assertEqual(job.status, "failed")
        self.assertFalse((destination / "a").exists())
        self.assertEqual((destination / "b").read_text(), "keep")

    def test_batch_preflight_rejects_duplicate_names_from_different_folders(self):
        first = self.root / "first"
        second = self.root / "second"
        destination = self.root / "dest"
        for folder in (first, second, destination):
            folder.mkdir()
        (first / "same.txt").write_text("first")
        (second / "same.txt").write_text("second")
        for operation in ("copy", "move"):
            job = Transfer([first / "same.txt", second / "same.txt"], destination,
                           operation=operation).run()
            self.assertEqual(job.status, "failed")
            self.assertIn("same destination name", job.error)
            self.assertFalse((destination / "same.txt").exists())
            self.assertTrue((first / "same.txt").exists())
            self.assertTrue((second / "same.txt").exists())

    def test_copy_to_edited_name_and_refuse_existing_name(self):
        source = self.root / "file1"
        source.write_text("copy me")
        copied = Transfer([source], self.root, target_name="file2").run()
        self.assertEqual(copied.status, "completed", copied.error)
        self.assertEqual((self.root / "file2").read_text(), "copy me")
        self.assertEqual(source.read_text(), "copy me")
        repeated = Transfer([source], self.root, target_name="file2").run()
        self.assertEqual(repeated.status, "failed")
        self.assertEqual((self.root / "file2").read_text(), "copy me")

    def test_overwrite_all_and_none_apply_to_later_files(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        for name in ("a", "b"):
            (source / name).write_text(f"new {name}")
            (target / name).write_text(f"old {name}")
        all_job = self.run_with_conflict_choice(
            Transfer([source / "a", source / "b"], target, interactive=True), "all")
        self.assertEqual(all_job.status, "completed", all_job.error)
        self.assertEqual([(target / name).read_text() for name in ("a", "b")], ["new a", "new b"])
        for name in ("a", "b"):
            (target / name).write_text(f"old {name}")
        none_job = self.run_with_conflict_choice(
            Transfer([source / "a", source / "b"], target, interactive=True), "none")
        self.assertEqual(none_job.status, "completed", none_job.error)
        self.assertEqual(none_job.skipped, 2)
        self.assertEqual([(target / name).read_text() for name in ("a", "b")], ["old a", "old b"])

    def test_no_skips_only_one_conflict_and_prompts_for_the_next(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        for name in ("a", "b"):
            (source / name).write_text(f"new {name}")
            (target / name).write_text(f"old {name}")
        job = Transfer([source / "a", source / "b"], target, interactive=True)
        worker = threading.Thread(target=job.run, daemon=True)
        worker.start()
        try:
            for expected, decision in (("a", "no"), ("b", "yes")):
                deadline = time.monotonic() + 3
                while (job.conflict is None or job.conflict.source.name != expected) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNotNone(job.conflict)
                self.assertEqual(job.conflict.source.name, expected)
                self.assertTrue(job.answer_conflict(job.conflict, decision))
            worker.join(3)
            self.assertFalse(worker.is_alive())
        finally:
            job.cancel.set()
            worker.join(1)
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual((target / "a").read_text(), "old a")
        self.assertEqual((target / "b").read_text(), "new b")

    def test_cancelling_while_waiting_for_overwrite_keeps_both_files(self):
        source, target_dir = self.root / "source", self.root / "target"
        target_dir.mkdir()
        source.write_text("new")
        (target_dir / "source").write_text("old")
        job = Transfer([source], target_dir, interactive=True)
        worker = threading.Thread(target=job.run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 3
        while job.conflict is None and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(job.conflict)
        job.cancel.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(source.read_text(), "new")
        self.assertEqual((target_dir / "source").read_text(), "old")

    def test_size_differs_skips_equal_sizes_and_replaces_other_files(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        (source / "a").write_text("new")
        (target / "a").write_text("old")
        (source / "b").write_text("newer")
        (target / "b").write_text("old")
        job = self.run_with_conflict_choice(
            Transfer([source / "a", source / "b"], target, interactive=True), "size_differs")
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.skipped, 1)
        self.assertEqual((target / "a").read_text(), "old")
        self.assertEqual((target / "b").read_text(), "newer")

    def test_directory_merge_prompts_for_nested_file_and_preserves_other_contents(self):
        source, target = self.root / "source", self.root / "target"
        (source / "nested").mkdir(parents=True)
        (target / "source/nested").mkdir(parents=True)
        (source / "nested/item").write_text("new")
        (target / "source/nested/item").write_text("old")
        (target / "source/keep").write_text("keep")
        job = self.run_with_conflict_choice(Transfer([source], target, interactive=True), "yes")
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual((target / "source/nested/item").read_text(), "new")
        self.assertEqual((target / "source/keep").read_text(), "keep")

    def test_move_skip_leaves_conflicting_source_in_place(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        (target / "source").mkdir(parents=True)
        (source / "conflict").write_text("new")
        (source / "fresh").write_text("move")
        (target / "source/conflict").write_text("old")
        job = self.run_with_conflict_choice(
            Transfer([source], target, operation="move", interactive=True), "no")
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual((target / "source/conflict").read_text(), "old")
        self.assertEqual((target / "source/fresh").read_text(), "move")
        self.assertEqual((source / "conflict").read_text(), "new")
        self.assertFalse((source / "fresh").exists())

    def test_move_yes_replaces_existing_file_and_abort_preserves_both(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        (source / "item").write_text("new")
        (target / "item").write_text("old")
        aborted = self.run_with_conflict_choice(
            Transfer([source / "item"], target, operation="move", interactive=True), "abort")
        self.assertEqual(aborted.status, "cancelled")
        self.assertEqual((source / "item").read_text(), "new")
        self.assertEqual((target / "item").read_text(), "old")
        moved = self.run_with_conflict_choice(
            Transfer([source / "item"], target, operation="move", interactive=True), "yes")
        self.assertEqual(moved.status, "completed", moved.error)
        self.assertEqual(moved.method, "rename")
        self.assertFalse((source / "item").exists())
        self.assertEqual((target / "item").read_text(), "new")

    def test_cross_filesystem_move_replaces_only_after_copy_is_published(self):
        source, target_dir = self.root / "source", self.root / "target"
        target_dir.mkdir()
        source.write_text("replacement")
        (target_dir / "source").write_text("original")
        native_replace = os.replace

        def different_filesystem(old, new):
            if Path(old) == source:
                raise OSError(errno.EXDEV, "other filesystem")
            return native_replace(old, new)

        with mock.patch("workspace.model.os.replace", side_effect=different_filesystem):
            job = self.run_with_conflict_choice(
                Transfer([source], target_dir, operation="move", interactive=True), "yes")
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.method, "copy and remove")
        self.assertFalse(source.exists())
        self.assertEqual((target_dir / "source").read_text(), "replacement")

    def test_older_and_smaller_policies_apply_to_following_conflicts(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        for name, source_text, target_text in (("a", "new a", "old a"),
                                               ("b", "new b", "old b")):
            (source / name).write_text(source_text)
            (target / name).write_text(target_text)
        os.utime(source / "a", ns=(2_000_000_000, 2_000_000_000))
        os.utime(target / "a", ns=(1_000_000_000, 1_000_000_000))
        os.utime(source / "b", ns=(1_000_000_000, 1_000_000_000))
        os.utime(target / "b", ns=(2_000_000_000, 2_000_000_000))
        older = self.run_with_conflict_choice(
            Transfer([source / "a", source / "b"], target, interactive=True), "older")
        self.assertEqual(older.status, "completed", older.error)
        self.assertEqual(older.skipped, 1)
        self.assertEqual((target / "a").read_text(), "new a")
        self.assertEqual((target / "b").read_text(), "old b")
        (target / "a").write_text("long old a")
        (target / "b").write_text("old")
        smaller = self.run_with_conflict_choice(
            Transfer([source / "a", source / "b"], target, interactive=True), "smaller")
        self.assertEqual(smaller.status, "completed", smaller.error)
        self.assertEqual(smaller.skipped, 1)
        self.assertEqual((target / "a").read_text(), "new a")
        self.assertEqual((target / "b").read_text(), "old")

    def test_same_filesystem_move_uses_atomic_rename_without_clobbering(self):
        source = self.root / "old"
        source.mkdir()
        (source / "item").write_text("keep")
        moved = Transfer([source], self.root, target_name="new", operation="move").run()
        self.assertEqual(moved.status, "completed", moved.error)
        self.assertEqual(moved.method, "rename")
        self.assertFalse(source.exists())
        self.assertEqual((self.root / "new/item").read_text(), "keep")
        another = self.root / "another"
        another.write_text("original")
        blocked = Transfer([another], self.root, target_name="new", operation="move").run()
        self.assertEqual(blocked.status, "failed")
        self.assertEqual(another.read_text(), "original")

    def test_cross_filesystem_move_falls_back_to_copy_then_remove(self):
        source = self.root / "file1"
        source.write_text("move me")
        with mock.patch("workspace.model.rename_noreplace", side_effect=OSError(errno.EXDEV, "other filesystem")):
            moved = Transfer([source], self.root, target_name="file2", operation="move").run()
        self.assertEqual(moved.status, "completed", moved.error)
        self.assertEqual(moved.method, "copy and remove")
        self.assertFalse(source.exists())
        self.assertEqual((self.root / "file2").read_text(), "move me")

    def test_cancelled_cross_filesystem_move_preserves_source(self):
        source = self.root / "big.bin"
        source.write_bytes(b"x" * 4_000_000)

        class CancelDuringRead:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 5

        with mock.patch("workspace.model.rename_noreplace", side_effect=OSError(errno.EXDEV, "other filesystem")):
            moved = Transfer([source], self.root, target_name="other.bin", operation="move",
                             cancel=CancelDuringRead()).run()
        self.assertEqual(moved.status, "cancelled")
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "other.bin").exists())

    def test_delete_directory_does_not_follow_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        target = self.root / "remove"
        target.mkdir()
        (target / "child.txt").write_text("remove")
        (target / "outside-link").symlink_to(outside, target_is_directory=True)
        deleted = Deletion([target]).run()
        self.assertEqual(deleted.status, "completed", deleted.error)
        self.assertEqual(deleted.files, 3)
        self.assertFalse(target.exists())
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_delete_can_abort_during_directory_traversal(self):
        target = self.root / "remove"
        target.mkdir()
        for index in range(5):
            (target / f"item-{index}").write_text(str(index))

        class CancelAfterOneItem:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 4

        deleted = Deletion([target], cancel=CancelAfterOneItem()).run()
        self.assertEqual(deleted.status, "cancelled")
        self.assertTrue(target.exists())
        self.assertGreaterEqual(deleted.files, 1)
        self.assertGreaterEqual(len(list(target.iterdir())), 1)

    def test_file_listing_has_stable_directory_first_order_and_hidden_toggle(self):
        (self.root / "z-folder").mkdir()
        (self.root / "a.txt").write_text("text")
        (self.root / ".hidden").write_text("hidden")
        self.assertEqual([r[0] for r in directory_entries(self.root)], ["z-folder", "a.txt"])
        self.assertEqual(len(directory_entries(self.root, True)), 3)
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        (self.root / "link").symlink_to("a.txt")
        entries = {row[0]: row for row in directory_entries(self.root)}
        self.assertTrue(entries["z-folder"][1])
        self.assertTrue(entries["link"][2])
        self.assertTrue(entries["run.sh"][4])
        self.assertFalse(entries["a.txt"][4])

    def test_panel_sort_keeps_directories_first_and_unknown_creation_last(self):
        rows = [
            ("z.txt", False, False, 2, False, 20, None),
            ("a.md", False, False, 9, False, 10, 100),
            ("folder", True, False, 0, False, 30, 90),
            ("b.txt", False, False, 4, False, 40, 110),
        ]
        names = lambda result: [row[0] for row in result]
        self.assertEqual(names(sort_directory_rows(rows, "size")),
                         ["folder", "z.txt", "b.txt", "a.md"])
        self.assertEqual(names(sort_directory_rows(rows, "name", True)),
                         ["folder", "z.txt", "b.txt", "a.md"])
        self.assertEqual(names(sort_directory_rows(rows, "created", True)),
                         ["folder", "b.txt", "a.md", "z.txt"])

    def test_creation_sort_uses_optional_creation_metadata(self):
        target = self.root / "created.txt"
        target.write_text("timestamp")
        row = directory_entries(self.root, include_created=True)[0]
        self.assertEqual(row[0], "created.txt")
        self.assertEqual(len(row), 7)
        self.assertTrue(row[6] is None or isinstance(row[6], (int, float)))

    def test_directory_listing_streams_batches_and_stops_after_cancellation(self):
        for index in range(300):
            (self.root / f"file-{index:03}").touch()
        batches = []
        rows = directory_entries(self.root, on_batch=batches.append)
        self.assertEqual(len(rows), 300)
        self.assertEqual(sum(map(len, batches)), 300)
        self.assertEqual([row[0] for row in rows], sorted(row[0] for row in rows))
        batches.clear()
        cancelled = directory_entries(self.root, on_batch=batches.append,
                                      cancelled=lambda: bool(batches))
        self.assertEqual(cancelled, [])
        self.assertEqual(len(batches), 1)

    def test_agent_client_rejects_remote_and_credential_bearing_origins(self):
        for origin in ("https://127.0.0.1:8098", "http://remote.invalid", "http://u:p@127.0.0.1", "http://127.0.0.1/api", "http://127.0.0.1#fragment"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Agents(origin)


if __name__ == "__main__":
    unittest.main()
