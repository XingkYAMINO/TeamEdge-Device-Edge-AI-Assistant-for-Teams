from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from local_workspace_service.main import create_app


@pytest.fixture
def client(tmp_path_factory):
    database = tmp_path_factory.mktemp("service-state") / "service.db"
    app = create_app(database)
    with TestClient(app) as test_client:
        yield test_client


def registration_payload(root, **overrides):
    payload = {
        "name": "student-team",
        "root_path": str(root),
        "allowed_extensions": [".md", ".docx"],
        "ignored_directories": [".git", "node_modules"],
        "ignored_files": [".env", "*secret*"],
        "limits": {"max_file_size_mb": 1, "max_files_per_scan": 100},
        "permissions": {
            "read": True,
            "write": False,
            "delete": False,
            "execute": False,
        },
    }
    payload.update(overrides)
    return payload

