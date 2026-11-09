from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_frontend_async_state_regressions():
    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            "node",
            "--test",
            "tests/frontend_state.test.cjs",
            "tests/model_connections_frontend.test.cjs",
            "tests/agent_frontend.test.cjs",
        ],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
