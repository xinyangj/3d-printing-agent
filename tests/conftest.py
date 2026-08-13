from __future__ import annotations

from pathlib import Path

import pytest

from printing_agent.config import Settings
from printing_agent.repositories import WorkflowRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=tmp_path / "agent.db",
        artifact_dir=tmp_path / "artifacts",
        candidate_cache_dir=tmp_path / "cache",
        simulator_spool_dir=tmp_path / "spool",
        thingiverse_token="test-token",
    )


@pytest.fixture
async def repository(settings: Settings) -> WorkflowRepository:
    value = WorkflowRepository(settings.database_url)
    await value.initialize()
    return value
