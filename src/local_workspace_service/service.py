from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from ._filesystem import open_root
from .errors import InvalidRootError, ScanInProgressError
from .models import (
    LatestScanResult,
    ScanResult,
    Workspace,
    WorkspaceCreate,
    WorkspacePolicy,
)
from .repository import WorkspaceRepository
from .scanner import scan_workspace


class WorkspaceService:
    def __init__(self, repository: WorkspaceRepository) -> None:
        self.repository = repository
        self._scan_guard = Lock()
        self._active_scans: set[str] = set()

    def register(self, request: WorkspaceCreate) -> Workspace:
        try:
            with open_root(request.root_path) as (_, root):
                pass
        except ValueError as exc:
            raise InvalidRootError(str(exc)) from exc
        except OSError as exc:
            raise InvalidRootError(
                "root_path must be an accessible local directory without reparse points"
            ) from exc

        now = datetime.now(timezone.utc)
        workspace = Workspace(
            workspace_id=str(uuid4()),
            name=request.name,
            root_path=str(root),
            allowed_extensions=request.allowed_extensions,
            ignored_directories=request.ignored_directories,
            ignored_files=request.ignored_files,
            limits=request.limits,
            permissions=request.permissions,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create(workspace)

    def get(self, workspace_id: str) -> Workspace:
        return self.repository.get(workspace_id)

    def list(self) -> list[Workspace]:
        return self.repository.list()

    def update_policy(
        self, workspace_id: str, policy: WorkspacePolicy
    ) -> Workspace:
        return self.repository.update_policy(
            workspace_id, policy, datetime.now(timezone.utc)
        )

    def scan(self, workspace_id: str) -> ScanResult:
        with self._scan_guard:
            if workspace_id in self._active_scans:
                raise ScanInProgressError(
                    "this workspace is already being scanned; wait for it to finish"
                )
            self._active_scans.add(workspace_id)
        try:
            # This immutable model is the scan-start snapshot. A policy update that
            # happens while scanning does not change what this scan uses.
            workspace = self.repository.get(workspace_id)
            policy_snapshot = WorkspacePolicy(
                allowed_extensions=workspace.allowed_extensions,
                ignored_directories=workspace.ignored_directories,
                ignored_files=workspace.ignored_files,
                limits=workspace.limits,
                permissions=workspace.permissions,
            )
            result = scan_workspace(workspace)
            self.repository.save_scan_result(
                workspace_id, result, policy_snapshot
            )
            return result
        finally:
            with self._scan_guard:
                self._active_scans.discard(workspace_id)

    def get_scan_result(self, workspace_id: str) -> LatestScanResult:
        return self.repository.get_scan_result(workspace_id)
