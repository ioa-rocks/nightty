# SPDX-License-Identifier: MPL-2.0
"""Filesystem views and explicit file operations; no agent execution."""

from dataclasses import dataclass, field
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import uuid


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class State:
    def __init__(self, root):
        self.path = Path(root) / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self, **changes):
        self.data.update(changes)
        atomic_json(self.path, self.data)

    @property
    def favourites(self):
        return [Path(p) for p in self.data.get("favourites", []) if isinstance(p, str)]

    @property
    def recent_folders(self):
        return [Path(p) for p in self.data.get("recent_folders", []) if isinstance(p, str)]

    def remember_folder(self, path):
        # Keep recent-folder updates independent of a potentially slow mount.
        path = os.path.abspath(os.fspath(path))
        recent = [path, *(str(folder) for folder in self.recent_folders if str(folder) != path)][:16]
        if recent != self.data.get("recent_folders", []):
            self.save(recent_folders=recent)

    def toggle_favourite(self, path):
        path = os.path.abspath(os.fspath(path))
        entries = [str(p) for p in self.favourites]
        if path in entries:
            entries.remove(path)
        else:
            entries.append(path)
        self.save(favourites=entries)


def metadata(folder):
    """Bounded view metadata only. Ignore unknown keys; never evaluate commands."""
    folder = Path(folder)
    path = folder / ".surface.json"
    if path.is_file():
        if path.stat().st_size > 65536:
            raise ValueError(".surface.json is too large")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(".surface.json must contain an object")
        return value
    readme = folder / "README.md"
    if readme.is_file():
        with readme.open() as stream:
            text = stream.read(65536)
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            import yaml
            value = yaml.safe_load(text[4:].split("\n---\n", 1)[0])
            return value if isinstance(value, dict) else {}
    return {}


def context_for(path):
    path = Path(path).resolve()
    chain, warnings, work = [], [], []
    agent = None
    for folder in reversed([path, *path.parents]):
        try:
            value = metadata(folder)
        except (OSError, ValueError) as exc:
            warnings.append(f"{folder}: {type(exc).__name__} reading project metadata")
            continue
        if value.get("type") == "project":
            chain.append({"path": str(folder), "title": str(value.get("title") or folder.name),
                          "description": str(value.get("description") or "").strip()})
        if isinstance(value.get("agent"), str):
            agent = value["agent"]
        references = value.get("work") or []
        if isinstance(references, list):
            for reference in references:
                if isinstance(reference, str):
                    target = (folder / reference).resolve()
                    if target not in work:
                        work.append(target)
    return {"projects": chain, "root": Path(chain[-1]["path"]) if chain else path,
            "title": chain[-1]["title"] if chain else path.name or str(path),
            "agent": agent, "work": work, "warnings": warnings}


def directory_entries(folder, hidden=False, on_batch=None, cancelled=None,
                      include_created=False):
    rows = []
    batch = []
    last_batch = time.monotonic()
    if include_created:
        from gi.repository import Gio, GLib
    with os.scandir(folder) as entries:
        for entry in entries:
            if cancelled is not None and cancelled():
                return []
            if not hidden and entry.name.startswith("."):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                is_directory = entry.is_dir()
                executable = stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
                created = getattr(info, "st_birthtime", None)
                if include_created and created is None:
                    try:
                        file_info = Gio.File.new_for_path(entry.path).query_info(
                            Gio.FILE_ATTRIBUTE_TIME_CREATED,
                            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
                        if file_info.has_attribute(Gio.FILE_ATTRIBUTE_TIME_CREATED):
                            created = file_info.get_attribute_uint64(Gio.FILE_ATTRIBUTE_TIME_CREATED)
                    except (GLib.Error, OSError):
                        pass
                row = (entry.name, is_directory, entry.is_symlink(), info.st_size,
                       executable, info.st_mtime, created)
            except OSError:
                row = (entry.name, False, entry.is_symlink(), 0, False, None, None)
            rows.append(row)
            if on_batch is not None:
                batch.append(row)
                if len(batch) >= 128 or time.monotonic() - last_batch >= 0.08:
                    on_batch(batch)
                    batch = []
                    last_batch = time.monotonic()
    if on_batch is not None and batch:
        on_batch(batch)
    return sorted(rows, key=lambda row: (not row[1], row[0].casefold()))


def sort_directory_rows(rows, criterion="name", reverse=False):
    """Keep folders first and missing timestamps last in either direction."""
    ordered = sorted(rows, key=lambda row: row[0].casefold())
    if criterion == "extension":
        ordered.sort(key=lambda row: Path(row[0]).suffix.casefold(), reverse=reverse)
    elif criterion in {"size", "modified", "created"}:
        index = {"size": 3, "modified": 5, "created": 6}[criterion]
        ordered.sort(key=lambda row: row[index] if row[index] is not None else 0,
                     reverse=reverse)
        ordered.sort(key=lambda row: row[index] is None)
    elif criterion == "name":
        if reverse:
            ordered.reverse()
    else:
        raise ValueError(f"Unknown sort criterion: {criterion}")
    ordered.sort(key=lambda row: not row[1])
    return ordered


class Cancelled(Exception):
    pass


def rename_noreplace(source, target):
    """Atomically rename without replacing a name created after preflight."""
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable", os.fspath(target))
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(target))


@dataclass
class TransferConflict:
    source: Path
    target: Path
    source_stat: os.stat_result
    target_stat: os.stat_result
    decision: str | None = None
    alerted: bool = False
    ready: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class Transfer:
    sources: list[Path]
    destination: Path
    status: str = "queued"
    files: int = 0
    bytes: int = 0
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    target_name: str | None = None
    operation: str = "copy"
    method: str = ""
    interactive: bool = False
    overwrite_policy: str = "ask"
    skipped: int = 0
    conflict: TransferConflict | None = field(default=None, init=False, repr=False)

    def answer_conflict(self, conflict, decision):
        if conflict is not self.conflict or conflict.ready.is_set():
            return False
        if decision not in {"yes", "no", "all", "none", "older", "smaller", "size_differs", "abort"}:
            raise ValueError(f"Unknown overwrite choice: {decision}")
        conflict.decision = decision
        conflict.ready.set()
        return True

    def _overwrite(self, source, target):
        if not target.exists() and not target.is_symlink():
            return False
        source_stat, target_stat = source.lstat(), target.lstat()
        if stat.S_ISDIR(source_stat.st_mode) or stat.S_ISDIR(target_stat.st_mode):
            raise ValueError(f"Cannot replace a directory with a file, or a file with a directory: {target}")
        if not (stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode)):
            raise ValueError(f"Cannot replace a special file: {target}")
        if not self.interactive:
            raise FileExistsError(f"Already exists: {target}")
        choice = self.overwrite_policy
        if choice == "ask":
            conflict = TransferConflict(source, target, source_stat, target_stat)
            self.conflict = conflict
            self.status = "conflict"
            try:
                while not conflict.ready.wait(0.1):
                    if self.cancel.is_set():
                        raise Cancelled()
                choice = conflict.decision
            finally:
                self.conflict = None
                self.status = "moving" if self.operation == "move" else "copying"
            if choice in {"all", "none", "older", "smaller", "size_differs"}:
                self.overwrite_policy = choice
        if self.cancel.is_set() or choice == "abort":
            raise Cancelled()
        if choice in {"yes", "all"}:
            return True
        if choice in {"no", "none"}:
            return None
        if choice == "older":
            return True if source_stat.st_mtime_ns > target_stat.st_mtime_ns else None
        if choice == "smaller":
            return True if source_stat.st_size < target_stat.st_size else None
        if choice == "size_differs":
            return True if source_stat.st_size != target_stat.st_size else None
        raise ValueError(f"Unknown overwrite choice: {choice}")

    def run(self):
        self.status = "moving" if self.operation == "move" else "copying"
        try:
            if self.operation not in {"copy", "move"}:
                raise ValueError("unknown transfer operation")
            if self.cancel.is_set():
                raise Cancelled()
            self.destination = self.destination.resolve(strict=True)
            if not self.destination.is_dir():
                raise ValueError("destination is not a directory")
            if self.target_name is not None:
                if len(self.sources) != 1 or self.target_name in {"", ".", ".."} or Path(self.target_name).name != self.target_name:
                    raise ValueError("a target name requires exactly one source and no path separators")
            # Validate every top-level destination before starting the batch.
            planned_targets = set()
            for source in self.sources:
                source = Path(source)
                source.lstat()
                target = self.destination / (self.target_name or source.name)
                if target in planned_targets:
                    raise ValueError(f"Multiple sources have the same destination name: {target.name}")
                planned_targets.add(target)
                if not self.interactive and (target.exists() or target.is_symlink()):
                    raise FileExistsError(f"Already exists: {target.name}")
                if (source.is_dir() and not source.is_symlink() and
                        (self.destination.is_relative_to(source.resolve()) or source == target)):
                    raise ValueError("cannot transfer a directory inside itself")
            for source in self.sources:
                source = Path(source)
                target = self.destination / (self.target_name or source.name)
                if self.cancel.is_set():
                    raise Cancelled()
                if self.operation == "move":
                    self._move(source, target)
                else:
                    self._copy(source, target)
            self.status = "completed"
        except Cancelled:
            self.status = "cancelled"
        except Exception as exc:
            self.status, self.error = "failed", str(exc)
        return self

    def _move(self, source, target):
        if source == target:
            raise ValueError(f"Source and destination are the same: {source}")
        info = source.lstat()
        target_exists = target.exists() or target.is_symlink()
        if stat.S_ISDIR(info.st_mode) and target_exists and target.is_dir() and not target.is_symlink():
            complete = True
            with os.scandir(source) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                if self.cancel.is_set():
                    raise Cancelled()
                if not self._move(child, target / child.name):
                    complete = False
            if complete:
                source.rmdir()
            self.method = "merge"
            return complete
        replace = self._overwrite(source, target) if target_exists else False
        if replace is None:
            self.skipped += 1
            return False
        # A rename is instantaneous on one filesystem; EXDEV covers mount edges.
        if info.st_dev == self.destination.stat().st_dev:
            try:
                if replace:
                    os.replace(source, target)
                else:
                    rename_noreplace(source, target)
                self.method = "rename"
                self.files += 1
                return True
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        self.method = "copy and remove"
        if stat.S_ISDIR(info.st_mode):
            target.mkdir()
            complete = True
            with os.scandir(source) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                if self.cancel.is_set():
                    raise Cancelled()
                if not self._move(child, target / child.name):
                    complete = False
            if complete:
                source.rmdir()
            return complete
        complete = self._copy(source, target, replace=replace)
        if self.cancel.is_set():
            raise Cancelled()
        if complete:
            source.unlink()
        return complete

    def _copy(self, source, target, replace=None):
        if self.cancel.is_set():
            raise Cancelled()
        info = source.lstat()
        if stat.S_ISDIR(info.st_mode):
            exists = target.exists() or target.is_symlink()
            if exists and (target.is_symlink() or not target.is_dir()):
                raise ValueError(f"Cannot merge a directory into a file: {target}")
            if not exists:
                target.mkdir()  # refuses a concurrent destination
            complete = True
            with os.scandir(source) as entries:
                for entry in entries:
                    if not self._copy(Path(entry.path), target / entry.name):
                        complete = False
            if not exists:
                shutil.copystat(source, target, follow_symlinks=False)
            return complete
        if replace is None:
            replace = self._overwrite(source, target)
        if replace is None:
            self.skipped += 1
            return False
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(source)
            if replace:
                temporary = target.parent / f".surface-copy-{uuid.uuid4().hex}"
                try:
                    temporary.symlink_to(link)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                target.symlink_to(link)
            self.files += 1
        elif stat.S_ISREG(info.st_mode):
            handle, temporary = tempfile.mkstemp(prefix=".surface-copy-", dir=target.parent)
            try:
                # Recheck the opened source, avoiding a symlink replacement race.
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as origin, os.fdopen(handle, "wb") as output:
                    handle = None
                    if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
                        raise ValueError("source changed to a special file")
                    while True:
                        if self.cancel.is_set():
                            raise Cancelled()
                        chunk = origin.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        self.bytes += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                shutil.copystat(source, temporary, follow_symlinks=False)
                # Keep publication atomic, including an approved replacement.
                if replace:
                    os.replace(temporary, target)
                else:
                    os.link(temporary, target)
                self.files += 1
            finally:
                if handle is not None:
                    os.close(handle)
                Path(temporary).unlink(missing_ok=True)
        else:
            raise ValueError(f"Special file is not copied: {source.name}")
        return True


@dataclass
class Deletion:
    """Delete depth-first, counting only items actually removed."""

    sources: list[Path]
    status: str = "queued"
    files: int = 0
    bytes: int = 0
    current: str = ""
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    operation: str = "delete"
    background: bool = False
    destination: Path | None = None
    target_name: str | None = None

    def run(self):
        self.status = "deleting"
        try:
            if not self.sources:
                raise ValueError("no items selected")
            for source in self.sources:
                source = Path(source)
                if source.parent == source or source.name in {"", ".", ".."}:
                    raise ValueError(f"Refusing to delete filesystem root: {source}")
                if self.cancel.is_set():
                    raise Cancelled()
                parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    self._delete(parent_fd, source.name, source)
                finally:
                    os.close(parent_fd)
            self.status = "completed"
        except Cancelled:
            self.status = "cancelled"
        except Exception as exc:
            self.status, self.error = "failed", str(exc)
        return self

    def _delete(self, parent_fd, name, path):
        if self.cancel.is_set():
            raise Cancelled()
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        self.current = str(path)
        if stat.S_ISDIR(info.st_mode):
            directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        self._delete(directory_fd, entry.name, path / entry.name)
            finally:
                os.close(directory_fd)
            if self.cancel.is_set():
                raise Cancelled()
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
            if stat.S_ISREG(info.st_mode):
                self.bytes += info.st_size
        self.files += 1
