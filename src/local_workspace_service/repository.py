from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .errors import WorkspaceConflictError, WorkspaceNotFoundError
from .models import LatestScanResult, ScanResult, Workspace, WorkspacePolicy


class WorkspaceRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    root_path_key TEXT NOT NULL UNIQUE,
                    policy_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS latest_scan_results (
                    workspace_id TEXT PRIMARY KEY,
                    scanned_at TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    FOREIGN KEY (workspace_id) REFERENCES workspaces(workspace_id)
                )
                """
            )

    def create(self, workspace: Workspace) -> Workspace:
        policy_json = self._policy_for(workspace).model_dump_json()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        workspace_id, name, root_path, root_path_key,
                        policy_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace.workspace_id,
                        workspace.name,
                        workspace.root_path,
                        self._path_key(workspace.root_path),
                        policy_json,
                        workspace.created_at.isoformat(),
                        workspace.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise WorkspaceConflictError(
                "this directory is already registered as a workspace"
            ) from exc
        return workspace

    def get(self, workspace_id: str) -> Workspace:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise WorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
        return self._row_to_workspace(row)

    def list(self) -> list[Workspace]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspaces
                ORDER BY name COLLATE NOCASE, created_at, workspace_id
                """
            ).fetchall()
        return [self._row_to_workspace(row) for row in rows]

    def update_policy(
        self, workspace_id: str, policy: WorkspacePolicy, updated_at: datetime
    ) -> Workspace:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE workspaces
                SET policy_json = ?, updated_at = ?
                WHERE workspace_id = ?
                """,
                (policy.model_dump_json(), updated_at.isoformat(), workspace_id),
            )
            if cursor.rowcount == 0:
                raise WorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return self._row_to_workspace(row)

    def save_scan_result(
        self,
        workspace_id: str,
        result: ScanResult,
        policy_snapshot: WorkspacePolicy,
    ) -> None:
        # Both JSON documents are fully serialized before the transaction starts.
        # The single UPSERT means readers see either the old complete result or the
        # new complete result, never a partially written scan.
        policy_json = policy_snapshot.model_dump_json()
        result_json = result.model_dump_json()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            )
            if cursor.fetchone() is None:
                raise WorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            connection.execute(
                """
                INSERT INTO latest_scan_results (
                    workspace_id, scanned_at, policy_json, result_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    scanned_at = excluded.scanned_at,
                    policy_json = excluded.policy_json,
                    result_json = excluded.result_json
                """,
                (
                    workspace_id,
                    result.scanned_at.isoformat(),
                    policy_json,
                    result_json,
                ),
            )

    def get_scan_result(self, workspace_id: str) -> LatestScanResult:
        workspace = self.get(workspace_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT policy_json, result_json
                FROM latest_scan_results
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
        if row is None:
            return LatestScanResult(status="never_scanned")

        policy_snapshot = WorkspacePolicy.model_validate_json(row["policy_json"])
        current_policy = self._policy_for(workspace)
        return LatestScanResult(
            status="available",
            result=ScanResult.model_validate_json(row["result_json"]),
            policy_snapshot=policy_snapshot,
            needs_rescan=policy_snapshot != current_policy,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _path_key(root_path: str) -> str:
        return os.path.normcase(os.path.normpath(root_path))

    @staticmethod
    def _row_to_workspace(row: sqlite3.Row) -> Workspace:
        policy = json.loads(row["policy_json"])
        return Workspace(
            workspace_id=row["workspace_id"],
            name=row["name"],
            root_path=row["root_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            **policy,
        )

    @staticmethod
    def _policy_for(workspace: Workspace) -> WorkspacePolicy:
        return WorkspacePolicy(
            allowed_extensions=workspace.allowed_extensions,
            ignored_directories=workspace.ignored_directories,
            ignored_files=workspace.ignored_files,
            limits=workspace.limits,
            permissions=workspace.permissions,
        )
