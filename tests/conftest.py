"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from archy.config import Settings
from archy.core.storage import Repository


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(
        mode="offline",
        db_path=tmp_path / "test.db",
        gemini_api_key="",
        openai_api_key="",
    )


@pytest.fixture
def repo(tmp_settings: Settings):
    tmp_settings.ensure_db_dir()
    r = Repository(tmp_settings.db_path)
    try:
        yield r
    finally:
        r.close()
