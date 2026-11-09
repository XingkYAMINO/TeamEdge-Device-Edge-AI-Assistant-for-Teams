from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import registration_payload
from local_workspace_service.main import create_app


def test_register_get_and_persist_normalized_config(client, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()

    response = client.post(
        "/api/v1/workspaces",
        json=registration_payload(root, allowed_extensions=["MD", ".docx", ".MD"]),
    )

    assert response.status_code == 201
    workspace = response.json()
    assert workspace["root_path"] == os.path.normpath(str(root.resolve()))
    assert workspace["allowed_extensions"] == [".md", ".docx"]
    assert workspace["created_at"].endswith("Z") or "+00:00" in workspace["created_at"]
    fetched = client.get(f"/api/v1/workspaces/{workspace['workspace_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == workspace


def test_registration_rejects_invalid_roots_duplicate_and_unsafe_permissions(
    client, tmp_path
):
    missing = tmp_path / "missing"
    assert (
        client.post(
            "/api/v1/workspaces", json=registration_payload(missing)
        ).status_code
        == 422
    )

    root = tmp_path / "workspace"
    root.mkdir()
    assert client.post(
        "/api/v1/workspaces", json=registration_payload(root)
    ).status_code == 201
    assert client.post(
        "/api/v1/workspaces", json=registration_payload(root)
    ).status_code == 409

    other = tmp_path / "other"
    other.mkdir()
    unsafe = registration_payload(other)
    unsafe["permissions"]["write"] = True
    response = client.post("/api/v1/workspaces", json=unsafe)
    assert response.status_code == 422


def test_update_policy_is_persisted(client, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = client.post(
        "/api/v1/workspaces", json=registration_payload(root)
    ).json()

    policy = {
        "allowed_extensions": [".md"],
        "ignored_directories": ["build"],
        "ignored_files": ["draft*"],
        "limits": {"max_file_size_mb": 2, "max_files_per_scan": 7},
        "permissions": {
            "read": True,
            "write": False,
            "delete": False,
            "execute": False,
        },
    }
    response = client.put(
        f"/api/v1/workspaces/{workspace['workspace_id']}/policy", json=policy
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["allowed_extensions"] == [".md"]
    assert updated["ignored_directories"] == ["build"]
    assert updated["updated_at"] >= workspace["updated_at"]
    fetched = client.get(f"/api/v1/workspaces/{workspace['workspace_id']}")
    assert fetched.json() == updated


def test_config_survives_application_restart(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    database = tmp_path / "persistent.db"
    with TestClient(create_app(database)) as first_client:
        workspace = first_client.post(
            "/api/v1/workspaces", json=registration_payload(root)
        ).json()

    with TestClient(create_app(database)) as second_client:
        response = second_client.get(
            f"/api/v1/workspaces/{workspace['workspace_id']}"
        )

    assert response.status_code == 200
    assert response.json() == workspace


def test_minimal_registration_uses_safe_defaults_without_scanning(client, tmp_path):
    root = tmp_path / "项目资料"
    root.mkdir()
    response = client.post("/api/v1/workspaces", json={"name": "项目资料", "root_path": str(root)})
    assert response.status_code == 201
    workspace = response.json()
    assert workspace["allowed_extensions"] == [".md", ".docx"]
    assert workspace["permissions"] == {"read": True, "write": False, "delete": False, "execute": False}
    assert ".git" in workspace["ignored_directories"]
    assert "*.key" in workspace["ignored_files"]
    assert client.get(f"/api/v1/workspaces/{workspace['workspace_id']}/scan-result").json()["status"] == "never_scanned"


def test_unknown_workspace_returns_404(client):
    assert client.get("/api/v1/workspaces/unknown").status_code == 404
    assert client.post("/api/v1/workspaces/unknown/scan").status_code == 404


def test_relative_root_is_rejected(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path("relative").mkdir()
    response = client.post(
        "/api/v1/workspaces", json=registration_payload("relative")
    )
    assert response.status_code == 422


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path syntax")
def test_remote_roots_are_rejected_before_opening_volume(client, monkeypatch):
    from local_workspace_service import _windows_fs

    def unexpected_open(*args, **kwargs):
        pytest.fail("a non-local volume must not be opened")

    monkeypatch.setattr(_windows_fs.Directory, "open_volume", unexpected_open)
    monkeypatch.setattr(_windows_fs, "_get_drive_type", lambda anchor: 4)
    for path in (r"\\server\share\project", r"\\?\UNC\server\share\project", r"Z:\project"):
        response = client.post("/api/v1/workspaces", json=registration_payload(path))
        assert response.status_code == 422
        assert "local Windows drive" in response.json()["detail"]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_rejects_junction_in_registered_path(client, tmp_path):
    import _winapi

    (tmp_path / "original" / "project").mkdir(parents=True)
    link = tmp_path / "linked"
    _winapi.CreateJunction(str(tmp_path / "original"), str(link))
    response = client.post(
        "/api/v1/workspaces", json=registration_payload(link / "project")
    )
    assert response.status_code == 422
