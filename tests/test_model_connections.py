from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from conftest import registration_payload
from fake_credentials import FakeCredentialStore
from local_workspace_service.main import create_app
from local_workspace_service.model_connection_models import (
    ModelConnectionCreate,
    ModelConnectionPatch,
)


def payload(**overrides):
    value = {
        "name": "校园模型",
        "protocol": "openai_responses",
        "base_url": "https://models.example.test/custom/v1/",
        "model": "example-model",
        "api_key": "fake-key-for-tests-only",
    }
    value.update(overrides)
    return value


@pytest.fixture
def model_setup(tmp_path):
    database = tmp_path / "service.db"
    credentials = FakeCredentialStore()
    with TestClient(create_app(database, credentials)) as client:
        yield client, credentials, database


def test_create_connection_stores_secret_outside_sqlite_and_returns_no_key(model_setup):
    client, credentials, database = model_setup
    response = client.post("/api/v1/model-connections", json=payload())

    assert response.status_code == 201
    result = response.json()
    assert result["has_api_key"] is True
    assert result["is_active"] is False
    assert "api_key" not in result
    assert "credential" not in result
    assert list(credentials.secrets.values()) == ["fake-key-for-tests-only"]
    assert b"fake-key-for-tests-only" not in database.read_bytes()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "   "},
        {"protocol": "openai"},
        {"base_url": "http://example.test/v1"},
        {"base_url": "https://user:pass@example.test/v1"},
        {"base_url": "https://example.test/v1?key=value"},
        {"base_url": "https://example.test/v1#fragment"},
        {"model": "  "},
        {"unexpected": "field"},
    ],
)
def test_invalid_fields_are_rejected(model_setup, changes):
    client, _, _ = model_setup
    response = client.post("/api/v1/model-connections", json=payload(**changes))
    assert response.status_code == 422


def test_http_is_allowed_only_for_explicit_loopback(model_setup):
    client, _, _ = model_setup
    response = client.post(
        "/api/v1/model-connections",
        json=payload(base_url="http://127.0.0.1:11434/api", api_key=""),
    )
    assert response.status_code == 201
    assert response.json()["base_url"] == "http://127.0.0.1:11434/api"


def test_names_are_trimmed_and_unique_case_insensitively(model_setup):
    client, _, _ = model_setup
    first = client.post(
        "/api/v1/model-connections", json=payload(name="  Campus AI  ")
    )
    duplicate = client.post(
        "/api/v1/model-connections",
        json=payload(name="campus ai", api_key="another-fake-key"),
    )
    assert first.json()["name"] == "Campus AI"
    assert duplicate.status_code == 409


def test_config_and_active_selection_survive_service_recreation(tmp_path):
    database = tmp_path / "persistent.db"
    credentials = FakeCredentialStore()
    with TestClient(create_app(database, credentials)) as first:
        created = first.post("/api/v1/model-connections", json=payload()).json()
        assert first.put(
            f"/api/v1/model-connections/{created['id']}/activate"
        ).status_code == 200

    with TestClient(create_app(database, credentials)) as second:
        listed = second.get("/api/v1/model-connections").json()
        fetched = second.get(
            f"/api/v1/model-connections/{created['id']}"
        ).json()

    assert listed["active_connection_id"] == created["id"]
    assert listed["items"][0]["is_active"] is True
    assert fetched["has_api_key"] is True
    assert all("api_key" not in item for item in listed["items"])


def test_edit_fields_and_blank_key_preserve_saved_secret(model_setup):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    reference = next(iter(credentials.secrets))

    response = client.patch(
        f"/api/v1/model-connections/{created['id']}",
        json={
            "name": "更新后的名称",
            "model": "new-model",
            "base_url": "https://models.example.test/preserved/path",
            "api_key": "",
        },
    )

    assert response.status_code == 200
    assert response.json()["name"] == "更新后的名称"
    assert response.json()["model"] == "new-model"
    assert credentials.secrets[reference] == "fake-key-for-tests-only"


def test_replace_and_explicitly_clear_api_key(model_setup):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    url = f"/api/v1/model-connections/{created['id']}"
    reference = next(iter(credentials.secrets))

    replaced = client.patch(url, json={"api_key": "replacement-fake-key"})
    assert replaced.status_code == 200
    assert credentials.secrets[reference] == "replacement-fake-key"
    assert replaced.json()["has_api_key"] is True

    cleared = client.patch(url, json={"clear_api_key": True})
    assert cleared.status_code == 200
    assert reference not in credentials.secrets
    assert cleared.json()["has_api_key"] is False


def test_new_key_and_clear_conflict_does_not_change_secret(model_setup):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    response = client.patch(
        f"/api/v1/model-connections/{created['id']}",
        json={"api_key": "conflicting-fake-key", "clear_api_key": True},
    )
    assert response.status_code == 422
    assert list(credentials.secrets.values()) == ["fake-key-for-tests-only"]


def test_validation_and_conflict_responses_never_echo_api_key(model_setup):
    client, _, _ = model_setup
    secret = "never-echo-this-fake-secret"
    invalid = client.post(
        "/api/v1/model-connections",
        json=payload(protocol="invalid-protocol", api_key=secret),
    )
    assert invalid.status_code == 422
    assert secret not in invalid.text

    assert client.post(
        "/api/v1/model-connections", json=payload(api_key=secret)
    ).status_code == 201
    duplicate = client.post(
        "/api/v1/model-connections", json=payload(api_key="another-secret")
    )
    assert duplicate.status_code == 409
    assert "another-secret" not in duplicate.text


def test_delete_non_active_keeps_selection_and_delete_active_clears_it(model_setup):
    client, credentials, _ = model_setup
    active = client.post(
        "/api/v1/model-connections", json=payload(name="Active")
    ).json()
    other = client.post(
        "/api/v1/model-connections", json=payload(name="Other", api_key="")
    ).json()
    client.put(f"/api/v1/model-connections/{active['id']}/activate")

    assert client.delete(
        f"/api/v1/model-connections/{other['id']}"
    ).status_code == 204
    assert client.get("/api/v1/model-connections").json()[
        "active_connection_id"
    ] == active["id"]

    assert client.delete(
        f"/api/v1/model-connections/{active['id']}"
    ).status_code == 204
    result = client.get("/api/v1/model-connections").json()
    assert result == {"items": [], "active_connection_id": None}
    assert credentials.secrets == {}


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/model-connections/missing"),
    ("patch", "/api/v1/model-connections/missing"),
    ("put", "/api/v1/model-connections/missing/activate"),
    ("delete", "/api/v1/model-connections/missing"),
])
def test_unknown_connection_returns_404(model_setup, method, path):
    client, _, _ = model_setup
    if method == "patch":
        response = client.patch(path, json={})
    else:
        response = getattr(client, method)(path)
    assert response.status_code == 404


def test_credential_write_failure_does_not_create_or_replace_config(model_setup):
    client, credentials, _ = model_setup
    credentials.fail_next = "set"
    failed_create = client.post("/api/v1/model-connections", json=payload())
    assert failed_create.status_code == 503
    assert client.get("/api/v1/model-connections").json()["items"] == []

    created = client.post("/api/v1/model-connections", json=payload()).json()
    original = next(iter(credentials.secrets.values()))
    credentials.fail_next = "set"
    failed_update = client.patch(
        f"/api/v1/model-connections/{created['id']}",
        json={"api_key": "new-fake-key"},
    )
    assert failed_update.status_code == 503
    assert next(iter(credentials.secrets.values())) == original


def test_credential_delete_failure_keeps_config_and_active_selection(model_setup):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    client.put(f"/api/v1/model-connections/{created['id']}/activate")
    credentials.fail_next = "delete"

    response = client.delete(f"/api/v1/model-connections/{created['id']}")

    assert response.status_code == 503
    saved = client.get("/api/v1/model-connections").json()
    assert saved["active_connection_id"] == created["id"]
    assert len(saved["items"]) == 1


def test_database_update_failure_restores_original_secret(model_setup, monkeypatch):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    service = client.app.state.model_connection_service
    reference = next(iter(credentials.secrets))
    original = credentials.secrets[reference]

    def fail_update(*args, **kwargs):
        raise RuntimeError("fake database failure")

    monkeypatch.setattr(service.repository, "update", fail_update)
    with pytest.raises(RuntimeError, match="fake database failure"):
        service.update(
            created["id"], ModelConnectionPatch(api_key="replacement-fake-key")
        )
    assert credentials.secrets[reference] == original


def test_database_create_failure_removes_temporary_secret(model_setup, monkeypatch):
    client, credentials, _ = model_setup
    service = client.app.state.model_connection_service

    def fail_create(*args, **kwargs):
        raise RuntimeError("fake database create failure")

    monkeypatch.setattr(service.repository, "create", fail_create)
    with pytest.raises(RuntimeError, match="fake database create failure"):
        service.create(ModelConnectionCreate(**payload()))
    assert credentials.secrets == {}


def test_database_delete_failure_restores_deleted_secret(model_setup, monkeypatch):
    client, credentials, _ = model_setup
    created = client.post("/api/v1/model-connections", json=payload()).json()
    service = client.app.state.model_connection_service
    reference = next(iter(credentials.secrets))
    original = credentials.secrets[reference]

    def fail_delete(*args, **kwargs):
        raise RuntimeError("fake database delete failure")

    monkeypatch.setattr(service.repository, "delete", fail_delete)
    with pytest.raises(RuntimeError, match="fake database delete failure"):
        service.delete(created["id"])
    assert credentials.secrets[reference] == original
    assert service.repository.get(created["id"]).id == created["id"]


def test_configuration_operations_do_not_make_network_connections(
    model_setup, monkeypatch
):
    client, _, _ = model_setup

    def unexpected_network(*args, **kwargs):
        pytest.fail("model connection settings must not access the network")

    monkeypatch.setattr(socket, "create_connection", unexpected_network)
    created = client.post(
        "/api/v1/model-connections", json=payload(api_key="")
    ).json()
    assert client.patch(
        f"/api/v1/model-connections/{created['id']}",
        json={"model": "offline-edit"},
    ).status_code == 200
    assert client.put(
        f"/api/v1/model-connections/{created['id']}/activate"
    ).status_code == 200


def test_model_schema_upgrade_preserves_existing_workspace_data(tmp_path):
    database = tmp_path / "existing.db"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    with TestClient(create_app(database, FakeCredentialStore())) as first:
        workspace = first.post(
            "/api/v1/workspaces", json=registration_payload(workspace_root)
        ).json()

    with TestClient(create_app(database, FakeCredentialStore())) as upgraded:
        assert upgraded.post(
            "/api/v1/model-connections", json=payload(api_key="")
        ).status_code == 201
        fetched = upgraded.get(
            f"/api/v1/workspaces/{workspace['workspace_id']}"
        )

    assert fetched.status_code == 200
    assert fetched.json()["root_path"] == workspace["root_path"]
