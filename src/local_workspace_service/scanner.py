from __future__ import annotations

import fnmatch
import os
import time
from collections.abc import Generator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from ._filesystem import Directory, DirectoryEntry, ReparsePointError, open_root
from .errors import RootChangedError
from .models import (
    DEFAULT_IGNORED_FILES,
    ExcludedEntry,
    ExclusionReason,
    IncludedFile,
    ScanResult,
    ScanSummary,
    Workspace,
)


@dataclass
class _DirectoryScan:
    directory: Directory
    path: PurePosixPath
    entries: Generator[DirectoryEntry, None, None]

    def close(self, *, close_directory: bool) -> None:
        self.entries.close()
        if close_directory:
            self.directory.close()


def scan_workspace(workspace: Workspace, *, deadline: float | None = None) -> ScanResult:
    with ExitStack() as handles:
        try:
            root, resolved = handles.enter_context(open_root(workspace.root_path))
        except (OSError, ValueError) as exc:
            raise RootChangedError(
                "the registered root is no longer accessible or now resolves elsewhere"
            ) from exc
        if os.path.normcase(str(resolved)) != os.path.normcase(workspace.root_path):
            raise RootChangedError("the registered root now resolves elsewhere")
        return _scan(root, workspace, deadline=deadline)


def _scan(root: Directory, workspace: Workspace, *, deadline: float | None = None) -> ScanResult:
    candidates: list[IncludedFile] = []
    excluded: list[ExcludedEntry] = []
    errors: list[ExcludedEntry] = []
    ignored_directories = {name.casefold() for name in workspace.ignored_directories}
    ignored_patterns = dict.fromkeys(
        pattern.casefold() for pattern in [*DEFAULT_IGNORED_FILES, *workspace.ignored_files]
    )
    allowed_extensions = set(workspace.allowed_extensions)
    max_size = workspace.limits.max_file_size_mb * 1024 * 1024
    visited = 0
    traversal_limit_reached = False

    pending = [_DirectoryScan(root, PurePosixPath(), root.entries())]
    try:
        while pending:
            if deadline is not None and time.monotonic() >= deadline:
                traversal_limit_reached = True
                break
            current = pending[-1]
            try:
                entry = next(current.entries, None)
            except OSError:
                errors.append(
                    ExcludedEntry(
                        relative_path=current.path.as_posix(),
                        entry_type="directory",
                        reason=ExclusionReason.ACCESS_ERROR,
                        detail="directory metadata could not be accessed",
                    )
                )
                entry = None
            if entry is None:
                finished = pending.pop()
                finished.close(close_directory=finished.directory is not root)
                continue
            if visited == workspace.limits.max_entries_per_scan:
                traversal_limit_reached = True
                break
            visited += 1

            path = current.path / entry.name
            relative_path = path.as_posix()
            if entry.kind == "link":
                excluded.append(_link_exclusion(relative_path))
                continue
            if entry.kind == "error":
                errors.append(
                    ExcludedEntry(
                        relative_path=relative_path,
                        entry_type="other",
                        reason=ExclusionReason.ACCESS_ERROR,
                        detail="entry metadata could not be accessed",
                    )
                )
                continue
            if entry.kind == "directory":
                if entry.name.casefold() in ignored_directories:
                    excluded.append(
                        ExcludedEntry(
                            relative_path=relative_path,
                            entry_type="directory",
                            reason=ExclusionReason.IGNORED_DIRECTORY,
                            detail="directory name matches an ignored rule",
                        )
                    )
                    continue
                try:
                    child = current.directory.open_child(entry.name)
                except ReparsePointError:
                    excluded.append(_link_exclusion(relative_path))
                except OSError:
                    errors.append(
                        ExcludedEntry(
                            relative_path=relative_path,
                            entry_type="directory",
                            reason=ExclusionReason.ACCESS_ERROR,
                            detail="directory could not be safely opened",
                        )
                    )
                else:
                    pending.append(_DirectoryScan(child, path, child.entries()))
                continue
            if entry.kind == "other":
                excluded.append(
                    ExcludedEntry(
                        relative_path=relative_path,
                        entry_type="other",
                        reason=ExclusionReason.NOT_A_REGULAR_FILE,
                        detail="entry is not a regular file",
                    )
                )
                continue

            extension = path.suffix.lower()
            matching_pattern = next(
                (
                    pattern for pattern in ignored_patterns
                    if fnmatch.fnmatchcase(entry.name.casefold(), pattern)
                ),
                None,
            )
            if matching_pattern is not None:
                reason = ExclusionReason.IGNORED_FILE_PATTERN
                detail = f"file name matches ignored pattern {matching_pattern!r}"
            elif extension not in allowed_extensions:
                reason = ExclusionReason.EXTENSION_NOT_ALLOWED
                detail = f"extension {extension or '<none>'!r} is not allowed"
            elif entry.size > max_size:
                reason = ExclusionReason.FILE_TOO_LARGE
                detail = f"size {entry.size} bytes exceeds the {max_size} byte limit"
            else:
                try:
                    modified = datetime.fromtimestamp(entry.modified, tz=timezone.utc)
                except (OSError, OverflowError, ValueError):
                    errors.append(
                        ExcludedEntry(
                            relative_path=relative_path,
                            entry_type="file",
                            reason=ExclusionReason.ACCESS_ERROR,
                            detail="file modification time is not representable",
                        )
                    )
                    continue
                candidates.append(
                    IncludedFile(
                        relative_path=relative_path,
                        name=entry.name,
                        extension=extension,
                        size_bytes=entry.size,
                        modified_at=modified,
                    )
                )
                continue
            excluded.append(
                ExcludedEntry(
                    relative_path=relative_path, entry_type="file", reason=reason, detail=detail
                )
            )
    finally:
        while pending:
            unfinished = pending.pop()
            unfinished.close(close_directory=unfinished.directory is not root)

    file_limit = workspace.limits.max_files_per_scan
    candidates.sort(key=_path_order)
    included = candidates[:file_limit]
    excluded.extend(
        ExcludedEntry(
            relative_path=item.relative_path,
            entry_type="file",
            reason=ExclusionReason.FILE_LIMIT_EXCEEDED,
            detail="file is allowed but the included-file count reached its limit",
        )
        for item in candidates[file_limit:]
    )
    excluded.sort(key=_path_order)
    errors.sort(key=_path_order)

    return ScanResult(
        workspace_id=workspace.workspace_id,
        root_path=workspace.root_path,
        scanned_at=datetime.now(timezone.utc),
        included=included,
        excluded=excluded,
        errors=errors,
        summary=ScanSummary(
            included=len(included),
            excluded=len(excluded),
            included_count=len(included),
            excluded_count=len(excluded),
            error_count=len(errors),
            visited_entries=visited,
            limit_reached=len(included) == file_limit or traversal_limit_reached,
            traversal_limit_reached=traversal_limit_reached,
        ),
    )


def _link_exclusion(relative_path: str) -> ExcludedEntry:
    return ExcludedEntry(
        relative_path=relative_path,
        entry_type="link",
        reason=ExclusionReason.LINK_NOT_FOLLOWED,
        detail="symbolic links and Windows reparse points are not followed",
    )


def _path_order(entry: IncludedFile | ExcludedEntry) -> tuple[str, str]:
    return entry.relative_path.casefold(), entry.relative_path
