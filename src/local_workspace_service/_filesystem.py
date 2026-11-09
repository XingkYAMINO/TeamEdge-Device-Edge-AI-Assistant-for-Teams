"""Resolve and enumerate workspace directories without following links."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from collections.abc import Generator, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Literal, NamedTuple, Protocol


class DirectoryEntry(NamedTuple):
    name: str
    kind: Literal["directory", "file", "link", "other", "error"]
    size: int = 0
    modified: float = 0


class ReparsePointError(OSError):
    pass


class UnsafeFileError(OSError):
    """The requested entry cannot be read within the workspace trust boundary."""


@dataclass(frozen=True)
class FileSnapshot:
    content: bytes
    size: int
    modified: float


class Directory(Protocol):
    def close(self) -> None: ...
    def open_child(self, name: str, *, ignore_case: bool = False) -> Directory: ...
    def entries(self) -> Generator[DirectoryEntry, None, None]: ...
    def read_file(self, name: str, *, max_bytes: int) -> FileSnapshot: ...
    def verify_child(self, name: str, child: Directory) -> None: ...


class _PosixDirectory:
    def __init__(self, path: str, parent_fd: int | None = None) -> None:
        self.fd = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )

    def close(self) -> None:
        os.close(self.fd)

    def open_child(self, name: str, *, ignore_case: bool = False) -> _PosixDirectory:
        return _PosixDirectory(name, self.fd)

    def verify_child(self, name: str, child: _PosixDirectory) -> None:
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not os.path.samestat(current, os.fstat(child.fd)):
            raise UnsafeFileError("directory was replaced during access")

    def entries(self) -> Generator[DirectoryEntry, None, None]:
        with os.scandir(self.fd) as entries:
            for entry in entries:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    yield DirectoryEntry(entry.name, "error")
                    continue
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    yield DirectoryEntry(entry.name, "link")
                elif stat.S_ISDIR(mode):
                    yield DirectoryEntry(entry.name, "directory")
                elif stat.S_ISREG(mode):
                    yield DirectoryEntry(entry.name, "file", metadata.st_size, metadata.st_mtime)
                else:
                    yield DirectoryEntry(entry.name, "other")

    def read_file(self, name: str, *, max_bytes: int) -> FileSnapshot:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeFileError("entry is not a regular file")
            if before.st_nlink != 1:
                raise UnsafeFileError("files with multiple hard links are not supported")
            if before.st_size > max_bytes:
                raise UnsafeFileError("file exceeds the configured size limit")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if not os.path.samestat(after, current):
                raise UnsafeFileError("file was replaced during access")
            if len(content) > max_bytes:
                raise UnsafeFileError("file exceeds the configured size limit")
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
                or after.st_nlink != 1
                or len(content) != after.st_size
            ):
                raise UnsafeFileError("file changed while it was being read")
            return FileSnapshot(content, after.st_size, after.st_mtime)
        finally:
            os.close(fd)


@contextmanager
def open_root(root_path: str, *, verify_identity: bool = False) -> Iterator[tuple[Directory, Path]]:
    supplied = Path(root_path)
    if not supplied.is_absolute() or "\0" in root_path:
        raise ValueError("root_path must be an absolute local path")
    path = Path(os.path.normpath(supplied))

    with ExitStack() as handles:
        if os.name == "nt":
            from ._windows_fs import Directory as WindowsDirectory
            from ._windows_fs import validate_drive

            validate_drive(path)
            parts = path.parts[1:]
            windows_directory = handles.enter_context(
                closing(WindowsDirectory.open_volume(path.anchor, enumerate_entries=not parts))
            )
            if parts:
                if any(":" in part for part in parts):
                    raise ValueError("root_path must identify a directory")
                windows_directory = handles.enter_context(
                    closing(windows_directory.open_child("\\".join(parts), ignore_case=True))
                )
            canonical = windows_directory.canonical_path()
            yield windows_directory, canonical
            if verify_identity and windows_directory.canonical_path() != canonical:
                raise UnsafeFileError("root directory was moved during access")
        else:
            directory = handles.enter_context(closing(_PosixDirectory(path.anchor)))
            parents = []
            for part in path.parts[1:]:
                child = handles.enter_context(closing(directory.open_child(part)))
                if verify_identity:
                    parents.append((directory, part, child))
                directory = child
            yield directory, path
            for parent, name, child in reversed(parents):
                parent.verify_child(name, child)
