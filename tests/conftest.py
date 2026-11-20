"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def notebook_output_dir(tmp_path, monkeypatch):
    """Save generated notebooks to a temp folder, not the real output/."""
    directory = tmp_path / "output"
    monkeypatch.setenv("NOTEBOOK_OUTPUT_DIR", str(directory))
    return directory
