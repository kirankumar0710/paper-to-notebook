"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def notebook_output_dir(tmp_path, monkeypatch):
    """Save generated notebooks to a temp folder, not the real output/."""
    directory = tmp_path / "output"
    monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(directory))
    return directory


@pytest.fixture(autouse=True)
def allow_test_host(monkeypatch):
    """FastAPI's TestClient sends Host: testserver; allow it in tests only.

    The real allowed hosts (loopback names plus [app] allowed_hosts) are
    tested in test_security.py.
    """
    import api.security as security

    real = security.load_allowed_hosts
    monkeypatch.setattr(
        security, "allowed_hosts", lambda: real() | {"testserver"}
    )
