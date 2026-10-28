# SPDX-License-Identifier: MPL-2.0
import os
from pathlib import Path
import tempfile
import unittest

from workspace.search import SearchCancelled, find_names


class SearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_live_search_finds_new_nested_files_without_following_links(self):
        nested = self.root / "nested"
        nested.mkdir()
        found = nested / "Needle-日本語.txt"
        found.write_text("fresh")
        (self.root / "shortcut").symlink_to(nested, target_is_directory=True)
        (self.root / ".needle-hidden").write_text("hidden")
        batches = []
        outcome = find_names("needle", self.root, on_batch=batches.append)
        self.assertEqual(outcome.count, 1)
        self.assertEqual([match.path for batch in batches for match in batch], [found])
        self.assertEqual(find_names("needle", self.root, hidden=True).count, 2)

    def test_live_search_limit_and_cancellation(self):
        for index in range(4):
            (self.root / f"match-{index}").touch()
        self.assertTrue(find_names("match", self.root, limit=1).truncated)

        class StopSoon:
            calls = 0

            def __call__(self):
                self.calls += 1
                return self.calls >= 3

        with self.assertRaises(SearchCancelled):
            find_names("match", self.root, cancelled=StopSoon())

    def test_indexed_search_parses_nul_paths_and_skips_stale_entries(self):
        found = self.root / "needle\nwith newline.txt"
        found.write_text("found")
        stale = self.root / "needle-gone.txt"
        fake = self.root / "fake-plocate"
        output = os.fsencode(found) + b"\0" + os.fsencode(stale) + b"\0"
        fake.write_text("#!/usr/bin/python3\nimport sys\nsys.stdout.buffer.write(" + repr(output) + ")\n")
        fake.chmod(0o755)
        batches = []
        outcome = find_names("needle", self.root, scope="indexed", on_batch=batches.append,
                             plocate_binary=str(fake))
        self.assertEqual(outcome.count, 1)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual([match.path for batch in batches for match in batch], [found])

    def test_indexed_search_treats_glob_characters_as_literal_text(self):
        found = self.root / "a*b.txt"
        found.touch()
        fake = self.root / "fake-plocate"
        fake.write_text("#!/usr/bin/python3\nimport os, sys\n"
                        "assert sys.argv[-1] == 'a\\\\*b'\n"
                        "sys.stdout.buffer.write(" + repr(os.fsencode(found) + b"\0") + ")\n")
        fake.chmod(0o755)
        batches = []
        outcome = find_names("a*b", self.root, scope="indexed", on_batch=batches.append,
                             plocate_binary=str(fake))
        self.assertEqual(outcome.count, 1)
        self.assertEqual(batches[0][0].path, found)


if __name__ == "__main__":
    unittest.main()
