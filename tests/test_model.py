# SPDX-License-Identifier: MPL-2.0
import json
import errno
import os
from pathlib import Path
import tempfile
import threading
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
