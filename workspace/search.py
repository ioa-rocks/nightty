# SPDX-License-Identifier: MPL-2.0
"""Cancellable, bounded filename search for Nightty's file panes."""

from dataclasses import dataclass
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import time


MAX_RESULTS = 5000


class SearchCancelled(Exception):
    pass


@dataclass(frozen=True)
class Match:
    path: Path
    directory: bool
    symlink: bool
    size: int
    executable: bool


@dataclass(frozen=True)
class Outcome:
    count: int
    truncated: bool
    skipped: int = 0


def describe(path):
    path = Path(path)
    info = path.lstat()
    return Match(path, stat.S_ISDIR(info.st_mode), stat.S_ISLNK(info.st_mode),
                 info.st_size, stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111))


def find_names(query, root, scope="tree", hidden=False, on_batch=None,
               cancelled=None, limit=MAX_RESULTS, plocate_binary=None):
    """Stream existing matching paths; scan the tree or query plocate's index."""
    query = query.strip()
    if not query:
        raise ValueError("Enter a filename to find.")
    if scope not in {"tree", "indexed"}:
        raise ValueError(f"Unknown search scope: {scope}")
    if limit < 1:
        raise ValueError("Result limit must be positive")
    folded = query.casefold()
    batch = []
    count = 0
    skipped = 0
    last_batch = time.monotonic()

    def check_cancelled():
        if cancelled is not None and cancelled():
            raise SearchCancelled()

    def flush():
        nonlocal batch, last_batch
        if batch and on_batch is not None:
            on_batch(batch)
        batch = []
        last_batch = time.monotonic()

    def add(path):
        nonlocal count, skipped
        check_cancelled()
        try:
            match = describe(path)
        except OSError:
            skipped += 1
            return False
        if count == limit:
            return True
        batch.append(match)
        count += 1
        if len(batch) >= 128 or time.monotonic() - last_batch >= 0.12:
            flush()
        return False

    if scope == "tree":
        root_path = Path(root).expanduser().resolve(strict=True)
        stack = [root_path]
        if not root_path.is_dir():
            raise NotADirectoryError(str(root_path))
        while stack:
            check_cancelled()
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        check_cancelled()
                        if not hidden and entry.name.startswith("."):
                            continue
                        try:
                            directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            skipped += 1
                            continue
                        if directory:
                            stack.append(Path(entry.path))
                        if folded in entry.name.casefold() and add(entry.path):
                            flush()
                            return Outcome(count, True, skipped)
            except OSError:
                if folder == root_path:
                    raise
                skipped += 1
    else:
        binary = plocate_binary or shutil.which("plocate")
        if binary is None:
            raise FileNotFoundError("plocate is not installed; choose Current tree (live).")
        # Keep the UI's "contains" meaning when a query has glob metacharacters.
        pattern = "".join("\\" + character if character in "*?[]\\" else character
                          for character in query)
        command = [binary, "-0", "-b", "-i", "-e", "-l", str(limit + 1), "--", pattern]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0)
        pending = b""
        truncated = False
        try:
            while True:
                check_cancelled()
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                parts = (pending + chunk).split(b"\0")
                pending = parts.pop()
                for raw in parts:
                    path = Path(os.fsdecode(raw))
                    if folded in path.name.casefold() and add(path):
                        truncated = True
                        break
                if truncated:
                    break
            if not truncated:
                error = process.stderr.read().decode("utf-8", "replace").strip()
                code = process.wait()
                if error:
                    raise RuntimeError(error)
                if code not in {0, 1}:
                    raise RuntimeError(f"plocate exited with status {code}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
            process.stderr.close()
        flush()
        return Outcome(count, truncated, skipped)
    flush()
    return Outcome(count, False, skipped)
