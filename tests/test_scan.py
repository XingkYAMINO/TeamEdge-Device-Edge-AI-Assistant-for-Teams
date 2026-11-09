from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import registration_payload


def _register(client, root: Path, **overrides) -> str:
    response = client.post(
        "/api/v1/workspaces", json=registration_payload(root, **overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()["workspace_id"]


def test_scan_includes_only_allowed_metadata_and_reports_exclusions(client, tmp_path):
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "notes" / "meeting.md").write_text("meeting body", encoding="utf-8")
    (root / "report.DOCX").write_bytes(b"not parsed")
    (root / "photo.png").write_bytes(b"png")
    (root / "team-secret.md").write_text("secret", encoding="utf-8")
    (root / ".env").write_text("TOKEN=x", encoding="utf-8")
    (root / ".git" / "hidden.md").write_text("ignored", encoding="utf-8")
    (root / "large.md").write_bytes(b"x" * (1024 * 1024 + 1))
    workspace_id = _register(client, root)

    response = client.post(f"/api/v1/workspaces/{workspace_id}/scan")

    assert response.status_code == 200
    result = response.json()
    included = {item["relative_path"]: item for item in result["included"]}
    assert set(included) == {"notes/meeting.md", "report.DOCX"}
    assert included["notes/meeting.md"]["size_bytes"] == len("meeting body")
    assert included["notes/meeting.md"]["name"] == "meeting.md"
    assert included["notes/meeting.md"]["status"] == "ready"
    assert included["notes/meeting.md"]["content_hash"] is None
    assert "content" not in included["notes/meeting.md"]
    excluded = {item["relative_path"]: item["reason"] for item in result["excluded"]}
    assert excluded[".git"] == "ignored_directory"
    assert ".git/hidden.md" not in excluded
    assert excluded["photo.png"] == "extension_not_allowed"
    assert excluded["team-secret.md"] == "ignored_file_pattern"
    assert excluded[".env"] == "ignored_file_pattern"
    assert excluded["large.md"] == "file_too_large"
    assert result["errors"] == []
    assert result["summary"] == {
        "included": 2,
        "excluded": 5,
        "included_count": 2,
        "excluded_count": 5,
        "error_count": 0,
        "visited_entries": 8,
        "limit_reached": False,
        "traversal_limit_reached": False,
    }


def test_max_files_is_deterministic_and_excess_files_are_excluded(client, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ["c.md", "a.md", "b.md"]:
        (root / name).write_text(name, encoding="utf-8")
    workspace_id = _register(
        client,
        root,
        limits={"max_file_size_mb": 1, "max_files_per_scan": 2},
    )

    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()

    assert [item["relative_path"] for item in result["included"]] == ["a.md", "b.md"]
    assert [(item["relative_path"], item["reason"]) for item in result["excluded"]] == [
        ("c.md", "file_limit_exceeded")
    ]
    assert result["summary"]["limit_reached"]
    assert not result["summary"]["traversal_limit_reached"]


def test_global_sorting_and_file_limit_across_directories(client, tmp_path):
    (tmp_path / "a").mkdir()
    for name in ("a/x.md", "a/x.pdf", "y.md", "z.md", "z.pdf"):
        (tmp_path / name).touch()
    workspace_id = _register(client, tmp_path, limits={"max_files_per_scan": 2})
    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()
    assert [item["relative_path"] for item in result["included"]] == ["a/x.md", "y.md"]
    assert [item["relative_path"] for item in result["excluded"]] == [
        "a/x.pdf", "z.md", "z.pdf",
    ]


def test_entry_budget_counts_excluded_files(client, tmp_path):
    for index in range(5):
        (tmp_path / f"{index}.pdf").touch()
    workspace_id = _register(client, tmp_path, limits={"max_entries_per_scan": 2})
    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()
    assert result["included"] == []
    assert len(result["excluded"]) == 2
    assert result["summary"]["visited_entries"] == 2
    assert result["summary"]["limit_reached"]
    assert result["summary"]["traversal_limit_reached"]


def test_sensitive_patterns_cannot_be_cleared_on_registration_or_update(client, tmp_path):
    for name in ("team-token.md", "team-secret.docx", "draft.md", "regular.md"):
        (tmp_path / name).touch()
    workspace_id = _register(client, tmp_path, ignored_files=[])
    url = f"/api/v1/workspaces/{workspace_id}"
    original = client.post(f"{url}/scan").json()
    assert [item["relative_path"] for item in original["included"]] == [
        "draft.md", "regular.md",
    ]
    assert client.put(f"{url}/policy", json={"ignored_files": ["draft*"]}).status_code == 200
    updated = client.post(f"{url}/scan").json()
    assert [item["relative_path"] for item in updated["included"]] == ["regular.md"]
    assert {item["relative_path"] for item in updated["excluded"]} == {
        "draft.md", "team-token.md", "team-secret.docx",
    }


def test_symlink_to_outside_is_never_followed(client, tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.md").write_text("outside", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this system: {exc}")
    workspace_id = _register(client, root)

    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()

    assert result["included"] == []
    assert len(result["excluded"]) == 1
    assert result["excluded"][0]["relative_path"] == "escape"
    assert result["excluded"][0]["reason"] == "link_not_followed"
    assert "private.md" not in str(result)


def test_scan_fails_closed_when_registered_root_disappears(client, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    workspace_id = _register(client, root)
    root.rmdir()

    response = client.post(f"/api/v1/workspaces/{workspace_id}/scan")

    assert response.status_code == 409
    assert "no longer accessible" in response.json()["detail"]


def test_scan_rejects_root_replaced_by_symlink(client, tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    workspace_id = _register(client, root)
    root.rmdir()
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(root))
    else:
        root.symlink_to(outside, target_is_directory=True)

    response = client.post(f"/api/v1/workspaces/{workspace_id}/scan")

    assert response.status_code == 409
    assert "resolves elsewhere" in response.json()["detail"]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_rejects_junctions_during_traversal(client, tmp_path, monkeypatch):
    import _winapi

    from local_workspace_service._windows_fs import Directory

    root = tmp_path / "project"
    outside = tmp_path / "project-backup"
    child = root / "candidate"
    child.mkdir(parents=True)
    outside.mkdir()
    (root / "safe.md").touch()
    (outside / "private.md").touch()
    _winapi.CreateJunction(str(outside), str(root / "static"))
    workspace_id = _register(client, root)
    original_open = Directory.open_child

    def replace_before_open(directory, name, **options):
        if name == "candidate":
            child.rename(tmp_path / "original")
            _winapi.CreateJunction(str(outside), str(child))
        return original_open(directory, name, **options)

    monkeypatch.setattr(Directory, "open_child", replace_before_open)
    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()
    assert [item["relative_path"] for item in result["included"]] == ["safe.md"]
    assert {item["relative_path"]: item["reason"] for item in result["excluded"]} == {
        "candidate": "link_not_followed",
        "static": "link_not_followed",
    }
    assert "private.md" not in str(result)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory handles")
def test_windows_holds_validated_directory_through_enumeration(client, tmp_path, monkeypatch):
    from local_workspace_service._windows_fs import Directory

    root = tmp_path / "project"
    child = root / "candidate"
    child.mkdir(parents=True)
    (child / "safe.md").touch()
    workspace_id = _register(client, root)
    original_entries = Directory.entries
    attempts = []

    def rename_before_enumeration(directory):
        if directory.canonical_path() == child:
            attempts.append(child)
            with pytest.raises(OSError):
                child.rename(tmp_path / "renamed")
        yield from original_entries(directory)

    monkeypatch.setattr(Directory, "entries", rename_before_enumeration)
    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()
    assert attempts == [child]
    assert [item["relative_path"] for item in result["included"]] == ["candidate/safe.md"]


@pytest.mark.skipif(os.name != "nt", reason="injects a Windows directory open error")
def test_directory_open_error_does_not_abort_scan(client, tmp_path, monkeypatch):
    from local_workspace_service._windows_fs import Directory

    (tmp_path / "unavailable").mkdir()
    (tmp_path / "safe.md").touch()
    workspace_id = _register(client, tmp_path)
    original_open = Directory.open_child

    def fail_one_directory(directory, name, **options):
        if name == "unavailable":
            raise PermissionError
        return original_open(directory, name, **options)

    monkeypatch.setattr(Directory, "open_child", fail_one_directory)
    result = client.post(f"/api/v1/workspaces/{workspace_id}/scan").json()
    assert [item["relative_path"] for item in result["included"]] == ["safe.md"]
    assert result["errors"][0]["relative_path"] == "unavailable"
    assert result["errors"][0]["reason"] == "access_error"
    assert result["summary"]["error_count"] == 1
