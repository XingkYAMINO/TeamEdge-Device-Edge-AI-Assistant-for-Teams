from __future__ import annotations

import ipaddress
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelProtocol(StrEnum):
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


def _clean_required(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    return cleaned


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_base_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("base_url must not be blank")
    if any(character.isspace() or ord(character) < 32 for character in cleaned):
        raise ValueError("base_url must not contain whitespace")
    if "\\" in cleaned:
        raise ValueError("base_url must use forward slashes")
    try:
        parsed = urlsplit(cleaned)
        # Accessing port performs its own range and syntax validation.
        parsed.port
    except ValueError as exc:
        raise ValueError("base_url contains an invalid host or port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be a complete HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain a username or password")
    if "?" in cleaned or "#" in cleaned:
        raise ValueError("base_url must not contain a query string or fragment")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ValueError("base_url must not contain relative path segments")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ValueError("HTTP is allowed only for an explicit local loopback address")
    return cleaned


def _validate_api_key(value: str | None) -> str | None:
    if value in {None, ""}:
        return value
    if len(value.encode("utf-16-le")) > 2560:
        raise ValueError("api_key is too long for the system credential store")
    return value


class ModelConnectionCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    protocol: ModelProtocol
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=1280, repr=False)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_required(value, "name")

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        return _clean_required(value, "model")

    _validate_url = field_validator("base_url")(validate_base_url)
    _validate_secret = field_validator("api_key")(_validate_api_key)


class ModelConnectionPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    protocol: ModelProtocol | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=1280, repr=False)
    clear_api_key: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_required(value, "name")

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str | None) -> str | None:
        return None if value is None else _clean_required(value, "model")

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return None if value is None else validate_base_url(value)

    _validate_secret = field_validator("api_key")(_validate_api_key)

    @model_validator(mode="after")
    def validate_update(self) -> "ModelConnectionPatch":
        for field_name in ("name", "protocol", "base_url", "model"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} may not be null")
        if self.clear_api_key and self.api_key not in {None, ""}:
            raise ValueError("api_key and clear_api_key cannot be submitted together")
        return self


class ModelConnection(StrictModel):
    id: str
    name: str
    protocol: ModelProtocol
    base_url: str
    model: str
    has_api_key: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ModelConnectionList(StrictModel):
    items: list[ModelConnection]
    active_connection_id: str | None
