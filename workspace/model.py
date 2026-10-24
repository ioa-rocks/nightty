"""Filesystem views and explicit background transfers; no agent execution."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading


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

    def toggle_favourite(self, path):
        path = str(Path(path).resolve())
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


def directory_entries(folder, hidden=False):
    rows = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not hidden and entry.name.startswith("."):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                is_directory = entry.is_dir()
                rows.append((entry.name, is_directory, entry.is_symlink(), info.st_size))
            except OSError:
                rows.append((entry.name, False, entry.is_symlink(), 0))
    return sorted(rows, key=lambda row: (not row[1], row[0].casefold()))


class Cancelled(Exception):
    pass


@dataclass
class Transfer:
    sources: list[Path]
    destination: Path
    status: str = "queued"
    files: int = 0
    bytes: int = 0
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def run(self):
        self.status = "copying"
        try:
            self.destination = self.destination.resolve(strict=True)
            if not self.destination.is_dir():
                raise ValueError("destination is not a directory")
            # Validate every top-level destination before starting the batch.
            for source in self.sources:
                source = Path(source)
                target = self.destination / source.name
                if target.exists() or target.is_symlink():
                    raise FileExistsError(f"Already exists: {target.name}")
                if source.is_dir() and not source.is_symlink() and self.destination.is_relative_to(source.resolve()):
                    raise ValueError("cannot copy a directory inside itself")
            for source in self.sources:
                self._copy(Path(source), self.destination / Path(source).name)
            self.status = "completed"
        except Cancelled:
            self.status = "cancelled"
        except Exception as exc:
            self.status, self.error = "failed", str(exc)
        return self

    def _copy(self, source, target):
        if self.cancel.is_set():
            raise Cancelled()
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode):
            target.symlink_to(os.readlink(source))
            self.files += 1
        elif stat.S_ISDIR(info.st_mode):
            target.mkdir()  # refuses a concurrent destination; no merges
            with os.scandir(source) as entries:
                for entry in entries:
                    self._copy(Path(entry.path), target / entry.name)
            shutil.copystat(source, target, follow_symlinks=False)
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
                # Atomic no-clobber publication on this destination filesystem.
                os.link(temporary, target)
                self.files += 1
            finally:
                if handle is not None:
                    os.close(handle)
                Path(temporary).unlink(missing_ok=True)
        else:
            raise ValueError(f"Special file is not copied: {source.name}")
