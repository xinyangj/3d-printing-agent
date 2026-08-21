from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PRINTING_AGENT_",
        extra="ignore",
    )

    data_dir: Path = Path("var")
    database_url: Path = Path("var/printing-agent.db")
    artifact_dir: Path = Path("var/artifacts")
    candidate_cache_dir: Path = Path("var/candidate-cache")
    simulator_spool_dir: Path = Path("var/simulator")
    slice_dir: Path = Path("var/slices")
    bambu_studio_path: str | None = None
    bambu_studio_resource_dir: Path | None = None
    bambu_studio_config_dir: Path | None = None
    bambu_cloud_credential_path: Path = Path(
        "var/secrets/bambu-cloud-credentials.json"
    )
    bambu_cloud_snapshot_timeout_seconds: int = Field(default=20, ge=5, le=60)
    bambu_cloud_snapshot_ttl_seconds: int = Field(default=60, ge=15, le=300)
    api_host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    thingiverse_token: str | None = None
    thingiverse_api_url: str = "https://api.thingiverse.com"
    openscad_path: str = "openscad"
    copilot_model: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    search_budget: int = Field(default=5, ge=1, le=20)
    inspection_budget: int = Field(default=8, ge=1, le=30)
    post_selection_failure_budget: int = Field(default=3, ge=1, le=10)
    generation_attempt_budget: int = Field(default=4, ge=1, le=10)
    max_search_results: int = Field(default=10, ge=1, le=25)
    max_gallery_images: int = Field(default=6, ge=1, le=12)
    max_download_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_image_pixels: int = Field(default=25_000_000, ge=1_000_000)
    openscad_timeout_seconds: int = Field(default=120, ge=5, le=600)

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.database_url.parent,
            self.artifact_dir,
            self.candidate_cache_dir,
            self.simulator_spool_dir,
            self.slice_dir,
            self.bambu_cloud_credential_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
