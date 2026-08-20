from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
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
    api_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    api_port: int = Field(default=8000, ge=1, le=65535)
    admin_api_token: SecretStr | None = None
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

    @field_validator("admin_api_token", mode="before")
    @classmethod
    def validate_admin_token(cls, value):
        if value is None or value == "":
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        stripped = raw.strip()
        if not stripped:
            raise ValueError(
                "PRINTING_AGENT_ADMIN_API_TOKEN cannot be whitespace"
            )
        if len(stripped) < 32:
            raise ValueError("PRINTING_AGENT_ADMIN_API_TOKEN must be at least 32 characters")
        return SecretStr(stripped)

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.database_url.parent,
            self.artifact_dir,
            self.candidate_cache_dir,
            self.simulator_spool_dir,
            self.slice_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
