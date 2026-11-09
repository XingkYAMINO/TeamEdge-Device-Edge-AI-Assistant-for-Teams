"""Open child directories relative to a validated handle on Windows."""

from __future__ import annotations

import ctypes as ct
from collections.abc import Generator
from ctypes import wintypes as wt
from pathlib import Path

from ._filesystem import DirectoryEntry, FileSnapshot, ReparsePointError, UnsafeFileError


kernel32 = ct.WinDLL("kernel32", use_last_error=True)
ntdll = ct.WinDLL("ntdll")

_INVALID_HANDLE = ct.c_void_p(-1).value
_LIST_DIRECTORY = 0x0001
_READ_DATA = 0x0001
_STAT_AND_SYNCHRONIZE = 0x0080 | 0x00100000
_SHARE_READ = 1  # Directory handles prevent delete/rename and write opens.
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_OPEN_EXISTING = 3
_DIRECTORY_FILE = 0x00000001
_NON_DIRECTORY_FILE = 0x00000040
_SYNCHRONOUS = 0x00000020
_CASE_INSENSITIVE = 0x40
_DONT_REPARSE = 0x1000
_REPARSE_POINT_ENCOUNTERED = 0xC000050B
_DIRECTORY_ATTRIBUTE = 0x10
_REPARSE_ATTRIBUTE = 0x400
_FILE_FULL_DIRECTORY_INFO = 14
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_BASIC_INFO = 0
_FILE_STANDARD_INFO = 1
_NO_MORE_FILES = 18
_LOCAL_DRIVE_TYPES = {2, 3, 5, 6}  # Removable, fixed, CD-ROM, RAM disk
_UNIX_EPOCH_FILETIME = 116444736000000000


class _UnicodeString(ct.Structure):
    _fields_ = [
        ("length", wt.USHORT),
        ("maximum_length", wt.USHORT),
        ("buffer", wt.LPWSTR),
    ]


class _ObjectAttributes(ct.Structure):
    _fields_ = [
        ("length", wt.ULONG),
        ("root_directory", wt.HANDLE),
        ("object_name", ct.POINTER(_UnicodeString)),
        ("attributes", wt.ULONG),
        ("security_descriptor", ct.c_void_p),
        ("security_quality_of_service", ct.c_void_p),
    ]


class _IoStatusBlock(ct.Structure):
    _fields_ = [("status", ct.c_void_p), ("information", ct.c_size_t)]


class _DirectoryInfo(ct.Structure):
    _fields_ = [
        ("next_offset", wt.DWORD),
        ("file_index", wt.DWORD),
        ("created", ct.c_int64),
        ("accessed", ct.c_int64),
        ("modified", ct.c_int64),
        ("changed", ct.c_int64),
        ("size", ct.c_int64),
        ("allocation_size", ct.c_int64),
        ("attributes", wt.DWORD),
        ("name_length", wt.DWORD),
        ("ea_size", wt.DWORD),
        ("name", wt.WCHAR * 1),
    ]


class _FileBasicInfo(ct.Structure):
    _fields_ = [
        ("created", ct.c_int64), ("accessed", ct.c_int64),
        ("modified", ct.c_int64), ("changed", ct.c_int64),
        ("attributes", wt.DWORD),
    ]


class _FileStandardInfo(ct.Structure):
    _fields_ = [
        ("allocation_size", ct.c_int64), ("end_of_file", ct.c_int64),
        ("number_of_links", wt.DWORD), ("delete_pending", ct.c_ubyte),
        ("directory", ct.c_ubyte),
    ]


def _api(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


_create_file = _api(
    kernel32, "CreateFileW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
    ct.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE,
)
_get_information = _api(
    kernel32, "GetFileInformationByHandleEx", wt.BOOL, wt.HANDLE, ct.c_int,
    ct.c_void_p, wt.DWORD,
)
_get_final_path = _api(
    kernel32, "GetFinalPathNameByHandleW", wt.DWORD, wt.HANDLE, wt.LPWSTR,
    wt.DWORD, wt.DWORD,
)
_get_drive_type = _api(kernel32, "GetDriveTypeW", wt.UINT, wt.LPCWSTR)
_close_handle = _api(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
_read_file = _api(
    kernel32, "ReadFile", wt.BOOL, wt.HANDLE, ct.c_void_p, wt.DWORD,
    ct.POINTER(wt.DWORD), ct.c_void_p,
)
_nt_open = _api(
    ntdll, "NtOpenFile", wt.LONG, ct.POINTER(wt.HANDLE), wt.DWORD,
    ct.POINTER(_ObjectAttributes), ct.POINTER(_IoStatusBlock), wt.ULONG, wt.ULONG,
)
_nt_error = _api(ntdll, "RtlNtStatusToDosError", wt.ULONG, wt.LONG)


def validate_drive(path: Path) -> None:
    drive = path.drive
    if (
        len(drive) != 2
        or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        or drive[1] != ":"
        or _get_drive_type(path.anchor) not in _LOCAL_DRIVE_TYPES
    ):
        raise ValueError("root_path must be on a local Windows drive")


class Directory:
    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def open_volume(cls, anchor: str, *, enumerate_entries: bool) -> Directory:
        access = _STAT_AND_SYNCHRONIZE | (_LIST_DIRECTORY if enumerate_entries else 0)
        handle = _create_file(
            "\\\\?\\" + anchor, access, _SHARE_READ, None, _OPEN_EXISTING,
            _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None,
        )
        if handle == _INVALID_HANDLE:
            raise ct.WinError(ct.get_last_error())
        return cls._checked(handle)

    def close(self) -> None:
        _close_handle(self.handle)

    def verify_child(self, name: str, child: Directory) -> None:
        # The handle also reveals short-name aliases that could bypass path rules.
        if child.canonical_path().name.casefold() != name.casefold():
            raise UnsafeFileError("directory name does not match the opened directory")

    def canonical_path(self) -> Path:
        buffer = ct.create_unicode_buffer(32768)
        length = _get_final_path(self.handle, buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise OSError("could not resolve the local directory handle")
        path = Path(buffer.value.removeprefix("\\\\?\\"))
        validate_drive(path)
        return path

    def open_child(self, name: str, *, ignore_case: bool = False) -> Directory:
        # NtOpenFile uses the validated parent object rather than looking its path up again.
        byte_length = len(name.encode("utf-16-le", errors="surrogatepass"))
        unicode_name = _UnicodeString(byte_length, byte_length + 2, name)
        attributes = _ObjectAttributes(
            ct.sizeof(_ObjectAttributes), self.handle, ct.pointer(unicode_name),
            _DONT_REPARSE | (_CASE_INSENSITIVE if ignore_case else 0), None, None,
        )
        handle = wt.HANDLE()
        status = _nt_open(
            ct.byref(handle), _STAT_AND_SYNCHRONIZE | _LIST_DIRECTORY, ct.byref(attributes),
            ct.byref(_IoStatusBlock()), _SHARE_READ,
            _DIRECTORY_FILE | _SYNCHRONOUS | _OPEN_REPARSE_POINT,
        )
        if (status & 0xFFFFFFFF) == _REPARSE_POINT_ENCOUNTERED:
            raise ReparsePointError("directory path traverses a Windows reparse point")
        if status < 0:
            raise ct.WinError(_nt_error(status))
        return Directory._checked(handle.value)

    @classmethod
    def _checked(cls, handle: int) -> Directory:
        directory = cls(handle)
        try:
            tag_info = (wt.DWORD * 2)()
            if not _get_information(
                handle, _FILE_ATTRIBUTE_TAG_INFO, ct.byref(tag_info), ct.sizeof(tag_info)
            ):
                raise ct.WinError(ct.get_last_error())
            if tag_info[0] & _REPARSE_ATTRIBUTE:
                raise ReparsePointError("directory is a Windows reparse point")
        except BaseException:
            directory.close()
            raise
        return directory

    def entries(self) -> Generator[DirectoryEntry, None, None]:
        # FileFullDirectoryInfo yields names and metadata from this exact directory.
        buffer = ct.create_string_buffer(16 * 1024)
        while _get_information(
            self.handle, _FILE_FULL_DIRECTORY_INFO, buffer, ct.sizeof(buffer)
        ):
            offset = 0
            while True:
                info = _DirectoryInfo.from_buffer(buffer, offset)
                name = ct.wstring_at(
                    ct.addressof(buffer) + offset + _DirectoryInfo.name.offset,
                    info.name_length // ct.sizeof(wt.WCHAR),
                )
                if name not in {".", ".."}:
                    if info.attributes & _REPARSE_ATTRIBUTE:
                        yield DirectoryEntry(name, "link")
                    elif info.attributes & _DIRECTORY_ATTRIBUTE:
                        yield DirectoryEntry(name, "directory")
                    else:
                        yield DirectoryEntry(
                            name, "file", info.size,
                            (info.modified - _UNIX_EPOCH_FILETIME) / 10_000_000,
                        )
                if not info.next_offset:
                    break
                offset += info.next_offset
        if (error := ct.get_last_error()) != _NO_MORE_FILES:
            raise ct.WinError(error)

    def read_file(self, name: str, *, max_bytes: int) -> FileSnapshot:
        byte_length = len(name.encode("utf-16-le", errors="surrogatepass"))
        unicode_name = _UnicodeString(byte_length, byte_length + 2, name)
        attributes = _ObjectAttributes(
            ct.sizeof(_ObjectAttributes), self.handle, ct.pointer(unicode_name),
            _DONT_REPARSE, None, None,
        )
        handle = wt.HANDLE()
        status = _nt_open(
            ct.byref(handle), _READ_DATA | _STAT_AND_SYNCHRONIZE,
            ct.byref(attributes), ct.byref(_IoStatusBlock()), _SHARE_READ,
            _NON_DIRECTORY_FILE | _SYNCHRONOUS | _OPEN_REPARSE_POINT,
        )
        if (status & 0xFFFFFFFF) == _REPARSE_POINT_ENCOUNTERED:
            raise ReparsePointError("file is a Windows reparse point")
        if status < 0:
            raise ct.WinError(_nt_error(status))
        try:
            tag_info = (wt.DWORD * 2)()
            if not _get_information(
                handle, _FILE_ATTRIBUTE_TAG_INFO, ct.byref(tag_info), ct.sizeof(tag_info)
            ):
                raise ct.WinError(ct.get_last_error())
            if tag_info[0] & _REPARSE_ATTRIBUTE:
                raise ReparsePointError("file is a Windows reparse point")
            if Directory(handle).canonical_path().name.casefold() != name.casefold():
                raise UnsafeFileError("file name does not match the opened file")
            before = self._file_info(handle)
            before_basic = self._basic_info(handle)
            if before.directory:
                raise UnsafeFileError("entry is not a regular file")
            if before.number_of_links != 1:
                raise UnsafeFileError("files with multiple hard links are not supported")
            if before.end_of_file > max_bytes:
                raise UnsafeFileError("file exceeds the configured size limit")
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                buffer = ct.create_string_buffer(min(1024 * 1024, max_bytes + 1 - total))
                read = wt.DWORD()
                if not _read_file(handle, buffer, len(buffer), ct.byref(read), None):
                    raise ct.WinError(ct.get_last_error())
                if read.value == 0:
                    break
                chunks.append(buffer.raw[:read.value])
                total += read.value
            after = self._file_info(handle)
            basic = self._basic_info(handle)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise UnsafeFileError("file exceeds the configured size limit")
            if (
                before.end_of_file != after.end_of_file
                or len(content) != after.end_of_file
                or after.number_of_links != 1
                or after.delete_pending
                or before_basic.modified != basic.modified
                or before_basic.changed != basic.changed
            ):
                raise UnsafeFileError("file changed while it was being read")
            return FileSnapshot(
                content, after.end_of_file,
                (basic.modified - _UNIX_EPOCH_FILETIME) / 10_000_000,
            )
        finally:
            _close_handle(handle)

    @staticmethod
    def _basic_info(handle: int) -> _FileBasicInfo:
        info = _FileBasicInfo()
        if not _get_information(handle, _FILE_BASIC_INFO, ct.byref(info), ct.sizeof(info)):
            raise ct.WinError(ct.get_last_error())
        return info

    @staticmethod
    def _file_info(handle: int) -> _FileStandardInfo:
        info = _FileStandardInfo()
        if not _get_information(
            handle, _FILE_STANDARD_INFO, ct.byref(info), ct.sizeof(info)
        ):
            raise ct.WinError(ct.get_last_error())
        return info
