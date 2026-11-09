from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from uuid import uuid4

from .credential_store import CredentialStore
from .errors import CredentialStoreError, ModelConnectionConflictError, ModelConnectionNotFoundError
from .model_connection_models import (
    ModelConnection,
    ModelConnectionCreate,
    ModelConnectionList,
    ModelConnectionPatch,
)
from .model_connection_repository import (
    ModelConnectionRecord,
    ModelConnectionRepository,
)
from .model_gateway import ModelTarget


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._configuration_lock:
            return method(self, *args, **kwargs)
    return call


class ModelConnectionService:
    _CREDENTIAL_PREFIX = "StudentUnion.LocalWorkspace.ModelConnection"

    def __init__(
        self,
        repository: ModelConnectionRepository,
        credential_store: CredentialStore,
    ) -> None:
        self.repository = repository
        self.credential_store = credential_store
        self._configuration_lock = RLock()

    def list(self) -> ModelConnectionList:
        records, active_id = self.repository.list()
        return ModelConnectionList(
            items=[self._public(record, active_id) for record in records],
            active_connection_id=active_id,
        )

    def get(self, connection_id: str) -> ModelConnection:
        return self._public(self.repository.get(connection_id), self.repository.active_id())

    def create(self, payload: ModelConnectionCreate) -> ModelConnection:
        if self.repository.name_exists(payload.name):
            raise ModelConnectionConflictError(
                "a model connection with this name already exists"
            )
        connection_id = str(uuid4())
        reference = f"{self._CREDENTIAL_PREFIX}.{connection_id}"
        secret = payload.api_key if payload.api_key not in {None, ""} else None
        if secret is not None:
            self.credential_store.set(reference, secret)

        now = datetime.now(timezone.utc)
        record = ModelConnectionRecord(
            id=connection_id,
            name=payload.name,
            protocol=payload.protocol,
            base_url=payload.base_url,
            model=payload.model,
            credential_reference=reference,
            has_api_key=secret is not None,
            created_at=now,
            updated_at=now,
        )
        try:
            self.repository.create(record)
        except Exception as exc:
            if secret is not None:
                self._compensate_delete(
                    reference,
                    "the configuration was not saved and its temporary credential "
                    "could not be removed",
                    exc,
                )
            raise
        return self._public(record, self.repository.active_id())

    @_synchronized
    def update(
        self, connection_id: str, payload: ModelConnectionPatch
    ) -> ModelConnection:
        current = self.repository.get(connection_id)
        if (
            "name" in payload.model_fields_set
            and payload.name != current.name
            and self.repository.name_exists(payload.name, excluding_id=connection_id)
        ):
            raise ModelConnectionConflictError(
                "a model connection with this name already exists"
            )

        replacement = payload.api_key if payload.api_key not in {None, ""} else None
        clear_secret = payload.clear_api_key and replacement is None
        touches_secret = replacement is not None or (clear_secret and current.has_api_key)
        old_secret: str | None = None
        if touches_secret and current.has_api_key:
            old_secret = self.credential_store.get(current.credential_reference)
            if old_secret is None:
                raise CredentialStoreError(
                    "the saved API key is missing from the system credential store"
                )

        new_has_secret = current.has_api_key
        if replacement is not None:
            self._replace_secret(current.credential_reference, replacement, old_secret)
            new_has_secret = True
        elif clear_secret and current.has_api_key:
            self.credential_store.delete(current.credential_reference)
            new_has_secret = False

        try:
            updated = self.repository.update(
                connection_id,
                name=payload.name if "name" in payload.model_fields_set else current.name,
                protocol=(
                    payload.protocol
                    if "protocol" in payload.model_fields_set
                    else current.protocol
                ),
                base_url=(
                    payload.base_url
                    if "base_url" in payload.model_fields_set
                    else current.base_url
                ),
                model=payload.model if "model" in payload.model_fields_set else current.model,
                has_api_key=new_has_secret,
                updated_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            if touches_secret:
                self._restore_secret(current.credential_reference, old_secret, exc)
            raise
        return self._public(updated, self.repository.active_id())

    @_synchronized
    def activate(self, connection_id: str) -> ModelConnection:
        record = self.repository.get(connection_id)
        self.repository.activate(connection_id)
        return self._public(record, connection_id)

    @_synchronized
    def delete(self, connection_id: str) -> None:
        record = self.repository.get(connection_id)
        old_secret: str | None = None
        if record.has_api_key:
            old_secret = self.credential_store.get(record.credential_reference)
            if old_secret is None:
                raise CredentialStoreError(
                    "the saved API key is missing from the system credential store"
                )
            self.credential_store.delete(record.credential_reference)
        try:
            self.repository.delete(connection_id)
        except Exception as exc:
            if old_secret is not None:
                self._restore_secret(record.credential_reference, old_secret, exc)
            raise

    @_synchronized
    def resolve_target(self, connection_id: str | None = None) -> ModelTarget:
        """Return an immutable execution snapshot, including the credential."""
        selected_id = connection_id or self.repository.active_id()
        if selected_id is None:
            raise ModelConnectionNotFoundError("no active model connection is selected")
        record = self.repository.get(selected_id)
        secret = None
        if record.has_api_key:
            secret = self.credential_store.get(record.credential_reference)
            if secret is None:
                raise CredentialStoreError(
                    "the saved API key is missing from the system credential store"
                )
        return ModelTarget(
            connection_id=record.id,
            connection_version=record.updated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            name=record.name,
            protocol=record.protocol,
            base_url=record.base_url,
            model=record.model,
            api_key=secret,
        )

    def _replace_secret(
        self, reference: str, replacement: str, old_secret: str | None
    ) -> None:
        try:
            self.credential_store.set(reference, replacement)
        except Exception as exc:
            self._restore_secret(reference, old_secret, exc)
            raise

    def _restore_secret(
        self, reference: str, old_secret: str | None, original: Exception
    ) -> None:
        try:
            if old_secret is None:
                self.credential_store.delete(reference)
            else:
                self.credential_store.set(reference, old_secret)
        except Exception as compensation_error:
            raise CredentialStoreError(
                "the operation failed and the previous credential could not be restored"
            ) from compensation_error

    def _compensate_delete(
        self, reference: str, message: str, original: Exception
    ) -> None:
        try:
            self.credential_store.delete(reference)
        except Exception as compensation_error:
            raise CredentialStoreError(message) from compensation_error

    @staticmethod
    def _public(record: ModelConnectionRecord, active_id: str | None) -> ModelConnection:
        return ModelConnection(
            id=record.id,
            name=record.name,
            protocol=record.protocol,
            base_url=record.base_url,
            model=record.model,
            has_api_key=record.has_api_key,
            is_active=record.id == active_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
