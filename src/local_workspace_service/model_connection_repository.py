from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import ModelConnectionConflictError, ModelConnectionNotFoundError
from .model_connection_models import ModelProtocol


@dataclass(frozen=True)
class ModelConnectionRecord:
    id: str
    name: str
    protocol: ModelProtocol
    base_url: str
    model: str
    credential_reference: str
    has_api_key: bool
    created_at: datetime
    updated_at: datetime


class ModelConnectionRepository:
    _ACTIVE_KEY = "active_model_connection_id"

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_connections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    protocol TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    credential_reference TEXT NOT NULL UNIQUE,
                    has_api_key INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    def create(self, record: ModelConnectionRecord) -> ModelConnectionRecord:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO model_connections (
                        id, name, name_key, protocol, base_url, model,
                        credential_reference, has_api_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.name,
                        self._name_key(record.name),
                        record.protocol.value,
                        record.base_url,
                        record.model,
                        record.credential_reference,
                        int(record.has_api_key),
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ModelConnectionConflictError(
                "a model connection with this name already exists"
            ) from exc
        return record

    def get(self, connection_id: str) -> ModelConnectionRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM model_connections WHERE id = ?", (connection_id,)
            ).fetchone()
        if row is None:
            raise ModelConnectionNotFoundError(
                f"model connection {connection_id!r} was not found"
            )
        return self._row_to_record(row)

    def list(self) -> tuple[list[ModelConnectionRecord], str | None]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM model_connections
                ORDER BY name COLLATE NOCASE, created_at, id
                """
            ).fetchall()
            active_id = self._active_id(connection)
        return [self._row_to_record(row) for row in rows], active_id

    def update(
        self,
        connection_id: str,
        *,
        name: str,
        protocol: ModelProtocol,
        base_url: str,
        model: str,
        has_api_key: bool,
        updated_at: datetime,
    ) -> ModelConnectionRecord:
        try:
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE model_connections
                    SET name = ?, name_key = ?, protocol = ?, base_url = ?,
                        model = ?, has_api_key = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        self._name_key(name),
                        protocol.value,
                        base_url,
                        model,
                        int(has_api_key),
                        updated_at.isoformat(),
                        connection_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ModelConnectionNotFoundError(
                        f"model connection {connection_id!r} was not found"
                    )
                row = connection.execute(
                    "SELECT * FROM model_connections WHERE id = ?", (connection_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ModelConnectionConflictError(
                "a model connection with this name already exists"
            ) from exc
        return self._row_to_record(row)

    def activate(self, connection_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM model_connections WHERE id = ?", (connection_id,)
            ).fetchone()
            if exists is None:
                raise ModelConnectionNotFoundError(
                    f"model connection {connection_id!r} was not found"
                )
            connection.execute(
                """
                INSERT INTO application_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (self._ACTIVE_KEY, connection_id),
            )

    def delete(self, connection_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM model_connections WHERE id = ?", (connection_id,)
            )
            if cursor.rowcount == 0:
                raise ModelConnectionNotFoundError(
                    f"model connection {connection_id!r} was not found"
                )
            connection.execute(
                """
                UPDATE application_settings SET value = NULL
                WHERE key = ? AND value = ?
                """,
                (self._ACTIVE_KEY, connection_id),
            )

    def active_id(self) -> str | None:
        with closing(self._connect()) as connection:
            return self._active_id(connection)

    def name_exists(self, name: str, excluding_id: str | None = None) -> bool:
        query = "SELECT 1 FROM model_connections WHERE name_key = ?"
        parameters: list[str] = [self._name_key(name)]
        if excluding_id is not None:
            query += " AND id <> ?"
            parameters.append(excluding_id)
        with closing(self._connect()) as connection:
            return connection.execute(query, parameters).fetchone() is not None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _active_id(self, connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT value FROM application_settings WHERE key = ?", (self._ACTIVE_KEY,)
        ).fetchone()
        return None if row is None else row["value"]

    @staticmethod
    def _name_key(name: str) -> str:
        return name.casefold()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ModelConnectionRecord:
        return ModelConnectionRecord(
            id=row["id"],
            name=row["name"],
            protocol=ModelProtocol(row["protocol"]),
            base_url=row["base_url"],
            model=row["model"],
            credential_reference=row["credential_reference"],
            has_api_key=bool(row["has_api_key"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
