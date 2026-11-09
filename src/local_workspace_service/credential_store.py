from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

from .errors import CredentialStoreError


class CredentialStore(Protocol):
    def get(self, reference: str) -> str | None: ...

    def set(self, reference: str, secret: str) -> None: ...

    def delete(self, reference: str) -> None: ...


class UnavailableCredentialStore:
    def _raise(self) -> None:
        raise CredentialStoreError(
            "the operating system credential store is unavailable on this platform"
        )

    def get(self, reference: str) -> str | None:
        self._raise()

    def set(self, reference: str, secret: str) -> None:
        self._raise()

    def delete(self, reference: str) -> None:
        self._raise()


if os.name == "nt":
    LPBYTE = ctypes.POINTER(wintypes.BYTE)

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialStore:
    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialStoreError(
                "Windows Credential Manager is unavailable on this platform"
            )
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_write = self._advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_read = self._advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_delete = self._advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = self._advapi32.CredFree
        self._cred_free.argtypes = [wintypes.LPVOID]
        self._cred_free.restype = None

    def get(self, reference: str) -> str | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._cred_read(
            reference, self._TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            self._raise_error("read", error)
        try:
            credential = pointer.contents
            blob = ctypes.string_at(
                credential.CredentialBlob, credential.CredentialBlobSize
            )
            return blob.decode("utf-16-le")
        finally:
            self._cred_free(pointer)

    def set(self, reference: str, secret: str) -> None:
        blob = secret.encode("utf-16-le")
        if len(blob) > 2560:
            raise CredentialStoreError(
                "the API key is too long for Windows Credential Manager"
            )
        buffer = ctypes.create_string_buffer(blob)
        credential = _CREDENTIALW()
        credential.Type = self._TYPE_GENERIC
        credential.TargetName = reference
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, LPBYTE)
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = "Local Workspace Service"
        if not self._cred_write(ctypes.byref(credential), 0):
            self._raise_error("write", ctypes.get_last_error())

    def delete(self, reference: str) -> None:
        if self._cred_delete(reference, self._TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self._ERROR_NOT_FOUND:
            self._raise_error("delete", error)

    @staticmethod
    def _raise_error(operation: str, error: int) -> None:
        raise CredentialStoreError(
            f"Windows Credential Manager could not {operation} the saved credential "
            f"(system error {error})"
        )


def create_system_credential_store() -> CredentialStore:
    if os.name == "nt":
        return WindowsCredentialStore()
    return UnavailableCredentialStore()
