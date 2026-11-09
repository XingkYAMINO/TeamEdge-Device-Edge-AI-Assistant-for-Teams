from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SUPPORTED_EXTENSIONS = frozenset({".md", ".docx"})
DEFAULT_IGNORED_DIRECTORIES = [
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    "__pycache__",
    ".venv",
]
DEFAULT_IGNORED_FILES = [".env", "*.key", "*.pem", "*secret*", "*token*"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanLimits(StrictModel):
    max_file_size_mb: Annotated[int, Field(ge=1, le=1024)] = 10
    max_files_per_scan: Annotated[int, Field(ge=1, le=100_000)] = 1000
    max_entries_per_scan: Annotated[int, Field(ge=1, le=100_000)] = 10_000


class WorkspacePermissions(StrictModel):
    read: bool = True
    write: bool = False
    delete: bool = False
    execute: bool = False

    @model_validator(mode="after")
    def only_safe_permissions_are_supported(self) -> "WorkspacePermissions":
        if not self.read or self.write or self.delete or self.execute:
            raise ValueError(
                "the first version requires read=true and write/delete/execute=false"
            )
        return self


def _validate_extensions(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        extension = value.strip().lower()
        if not extension.startswith("."):
            extension = f".{extension}"
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"unsupported extension {value!r}; only .md and .docx are supported"
            )
        if extension not in normalized:
            normalized.append(extension)
    if not normalized:
        raise ValueError("at least one allowed extension is required")
    return normalized


def _validate_directory_names(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        name = value.strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("ignored directories must be non-empty base names")
        if name.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(name)
    return normalized


def _validate_file_patterns(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        pattern = value.strip()
        if not pattern or "/" in pattern or "\\" in pattern:
            raise ValueError("ignored file patterns must match a base name only")
        if pattern.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(pattern)
    return normalized


class ScanPolicyFields(StrictModel):
    allowed_extensions: list[str] = Field(default_factory=lambda: [".md", ".docx"])
    ignored_directories: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IGNORED_DIRECTORIES)
    )
    ignored_files: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORED_FILES))
    limits: ScanLimits = Field(default_factory=ScanLimits)
    permissions: WorkspacePermissions = Field(default_factory=WorkspacePermissions)

    _normalize_extensions = field_validator("allowed_extensions")(_validate_extensions)
    _normalize_directories = field_validator("ignored_directories")(
        _validate_directory_names
    )
    _normalize_file_patterns = field_validator("ignored_files")(
        _validate_file_patterns
    )


class WorkspaceCreate(ScanPolicyFields):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    root_path: Annotated[str, Field(min_length=1, max_length=4096)]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class WorkspacePolicy(ScanPolicyFields):
    pass


class Workspace(WorkspaceCreate):
    workspace_id: str
    created_at: datetime
    updated_at: datetime


class ExclusionReason(StrEnum):
    IGNORED_DIRECTORY = "ignored_directory"
    IGNORED_FILE_PATTERN = "ignored_file_pattern"
    EXTENSION_NOT_ALLOWED = "extension_not_allowed"
    FILE_TOO_LARGE = "file_too_large"
    FILE_LIMIT_EXCEEDED = "file_limit_exceeded"
    LINK_NOT_FOLLOWED = "link_not_followed"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    ACCESS_ERROR = "access_error"


class IncludedFile(StrictModel):
    relative_path: str
    name: str
    extension: str
    size_bytes: int
    modified_at: datetime
    status: Literal["ready"] = "ready"
    content_hash: str | None = None


class ExcludedEntry(StrictModel):
    relative_path: str
    entry_type: Literal["file", "directory", "link", "other"]
    reason: ExclusionReason
    detail: str


class ScanSummary(StrictModel):
    included: int
    excluded: int
    included_count: int
    excluded_count: int
    error_count: int
    visited_entries: int
    limit_reached: bool
    traversal_limit_reached: bool


class ScanResult(StrictModel):
    workspace_id: str
    root_path: str
    scanned_at: datetime
    included: list[IncludedFile]
    excluded: list[ExcludedEntry]
    errors: list[ExcludedEntry]
    summary: ScanSummary


class LatestScanResult(StrictModel):
    status: Literal["never_scanned", "available"]
    result: ScanResult | None = None
    policy_snapshot: WorkspacePolicy | None = None
    needs_rescan: bool = False
