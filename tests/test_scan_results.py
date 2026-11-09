from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
from threading import Event

from fastapi.testclient import TestClient

from conftest import registration_payload
from local_workspace_service import service as service_module
from local_workspace_service.main import create_app
from local_workspace_service.models import (
    ExcludedEntry,
    ExclusionReason,
    ScanResult,
    ScanSummary,
)


def _register(client, root, **overrides):
    response = client.post(
        "/api/v1/workspaces", json=registration_payload(root, **overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_workspace_list_returns_registered_items_in_stable_order(client, tmp_path):
    second = tmp_path / "second"
    first = tmp_path / "first"
    second.mkdir()
    first.mkdir()
    _register(client, second, name="乙工作区")
    _register(client, first, name="甲工作区")

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["乙工作区", "甲工作区"]
    assert all("root_path" in item for item in response.json())


def test_never_scanned_and_successfully_scanned_empty_are_distinct(client, tmp_path):
    workspace = _register(client, tmp_path)
    url = f"/api/v1/workspaces/{workspace['workspace_id']}/scan-result"

    before = client.get(url)
    assert before.status_code == 200
    assert before.json() == {
        "status": "never_scanned",
        "result": None,
        "policy_snapshot": None,
        "needs_rescan": False,
    }

    assert client.post(url.removesuffix("-result")).status_code == 200
    after = client.get(url).json()
    assert after["status"] == "available"
    assert after["result"]["included"] == []
    assert after["result"]["summary"]["visited_entries"] == 0


def test_scan_result_survives_service_recreation_and_legacy_upgrade(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "saved.md").touch()
    database = tmp_path / "persistent.db"

    with TestClient(create_app(database)) as first_client:
        workspace = _register(first_client, root)
        scan = first_client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/scan"
        ).json()

    with TestClient(create_app(database)) as second_client:
        response = second_client.get(
            f"/api/v1/workspaces/{workspace['workspace_id']}/scan-result"
        )

    assert response.status_code == 200
    assert response.json()["result"] == scan


def test_existing_workspace_database_is_upgraded_without_data_loss(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    database = tmp_path / "legacy.db"
    with TestClient(create_app(database)) as client:
        workspace = _register(client, root)

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE latest_scan_results")

    with TestClient(create_app(database)) as upgraded_client:
        listed = upgraded_client.get("/api/v1/workspaces").json()
        latest = upgraded_client.get(
            f"/api/v1/workspaces/{workspace['workspace_id']}/scan-result"
        ).json()

    assert [item["workspace_id"] for item in listed] == [workspace["workspace_id"]]
    assert latest["status"] == "never_scanned"


def test_new_scan_replaces_old_result_and_failed_scan_keeps_it(client, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "first.md").touch()
    workspace = _register(client, root)
    base = f"/api/v1/workspaces/{workspace['workspace_id']}"

    first = client.post(f"{base}/scan").json()
    (root / "second.md").touch()
    second = client.post(f"{base}/scan").json()
    assert [item["name"] for item in first["included"]] == ["first.md"]
    assert [item["name"] for item in second["included"]] == ["first.md", "second.md"]
    assert client.get(f"{base}/scan-result").json()["result"] == second

    (root / "first.md").unlink()
    (root / "second.md").unlink()
    root.rmdir()
    failed = client.post(f"{base}/scan")
    assert failed.status_code == 409
    assert client.get(f"{base}/scan-result").json()["result"] == second


def test_policy_change_marks_saved_result_as_needing_rescan(client, tmp_path):
    workspace = _register(client, tmp_path)
    base = f"/api/v1/workspaces/{workspace['workspace_id']}"
    assert client.post(f"{base}/scan").status_code == 200
    assert client.get(f"{base}/scan-result").json()["needs_rescan"] is False

    response = client.put(
        f"{base}/policy",
        json={"allowed_extensions": [".md"], "ignored_files": ["draft*"]},
    )
    assert response.status_code == 200
    saved = client.get(f"{base}/scan-result").json()
    assert saved["needs_rescan"] is True
    assert saved["policy_snapshot"]["allowed_extensions"] == [".md", ".docx"]


def test_policy_update_during_scan_marks_result_stale(client, tmp_path, monkeypatch):
    (tmp_path / "file.md").touch()
    workspace = _register(client, tmp_path)
    base = f"/api/v1/workspaces/{workspace['workspace_id']}"
    entered = Event()
    release = Event()
    original_scan = service_module.scan_workspace

    def slow_scan(snapshot):
        entered.set()
        assert release.wait(5)
        return original_scan(snapshot)

    monkeypatch.setattr(service_module, "scan_workspace", slow_scan)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, f"{base}/scan")
        assert entered.wait(5)
        updated = client.put(f"{base}/policy", json={"allowed_extensions": [".md"]})
        assert updated.status_code == 200
        release.set()
        assert future.result(timeout=5).status_code == 200

    saved = client.get(f"{base}/scan-result").json()
    assert saved["needs_rescan"] is True
    assert saved["policy_snapshot"]["allowed_extensions"] == [".md", ".docx"]


def test_exclusions_errors_and_truncation_round_trip(client, tmp_path, monkeypatch):
    workspace = _register(client, tmp_path)
    base = f"/api/v1/workspaces/{workspace['workspace_id']}"

    def synthetic_scan(snapshot):
        return ScanResult(
            workspace_id=snapshot.workspace_id,
            root_path=snapshot.root_path,
            scanned_at=datetime.now(timezone.utc),
            included=[],
            excluded=[
                ExcludedEntry(
                    relative_path="ignored",
                    entry_type="directory",
                    reason=ExclusionReason.IGNORED_DIRECTORY,
                    detail="test exclusion",
                )
            ],
            errors=[
                ExcludedEntry(
                    relative_path="unavailable",
                    entry_type="directory",
                    reason=ExclusionReason.ACCESS_ERROR,
                    detail="test error",
                )
            ],
            summary=ScanSummary(
                included=0,
                excluded=1,
                included_count=0,
                excluded_count=1,
                error_count=1,
                visited_entries=2,
                limit_reached=True,
                traversal_limit_reached=True,
            ),
        )

    monkeypatch.setattr(service_module, "scan_workspace", synthetic_scan)
    original = client.post(f"{base}/scan").json()
    saved = client.get(f"{base}/scan-result").json()["result"]
    assert saved == original
    assert saved["excluded"][0]["reason"] == "ignored_directory"
    assert saved["errors"][0]["reason"] == "access_error"
    assert saved["summary"]["traversal_limit_reached"] is True


def test_second_concurrent_scan_is_rejected(client, tmp_path, monkeypatch):
    (tmp_path / "file.md").touch()
    workspace = _register(client, tmp_path)
    scan_url = f"/api/v1/workspaces/{workspace['workspace_id']}/scan"
    entered = Event()
    release = Event()
    original_scan = service_module.scan_workspace

    def slow_scan(snapshot):
        entered.set()
        assert release.wait(5)
        return original_scan(snapshot)

    monkeypatch.setattr(service_module, "scan_workspace", slow_scan)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, scan_url)
        assert entered.wait(5)
        second = client.post(scan_url)
        assert second.status_code == 409
        assert "already being scanned" in second.json()["detail"]
        release.set()
        assert first.result(timeout=5).status_code == 200


def test_unknown_workspace_scan_result_returns_404(client):
    response = client.get("/api/v1/workspaces/unknown/scan-result")
    assert response.status_code == 404
