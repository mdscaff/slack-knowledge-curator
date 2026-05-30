"""Configuration loaded from environment / .env via pydantic-settings.

Every stage reads from a single ``Settings`` instance so secrets live only in
``.env``. Stage-specific helpers (``require_slack``, ``require_anthropic``)
raise early with an actionable message instead of failing deep in an API call.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Slack ──────────────────────────────────────────────────────────────
    slack_token: str = Field(default="", alias="SLACK_TOKEN")
    slack_channels: str = Field(default="", alias="SLACK_CHANNELS")

    # ── Anthropic ──────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-haiku-4-5", alias="ANTHROPIC_MODEL")
    anthropic_max_concurrency: int = Field(default=4, alias="ANTHROPIC_MAX_CONCURRENCY")

    # ── X.com enrichment ─────────────────────────────────────────────────────
    x_oembed_timeout: float = Field(default=10.0, alias="X_OEMBED_TIMEOUT")
    x_enrich_non_x_links: bool = Field(default=True, alias="X_ENRICH_NON_X_LINKS")

    # ── Paths / behavior ─────────────────────────────────────────────────────
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Derived paths ────────────────────────────────────────────────────────
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def enriched_dir(self) -> Path:
        return self.data_dir / "enriched"

    @property
    def classified_dir(self) -> Path:
        return self.data_dir / "classified"

    @property
    def cursor_path(self) -> Path:
        return self.data_dir / "cursors.json"

    @property
    def taxonomy_path(self) -> Path:
        return self.data_dir / "taxonomy.json"

    @property
    def channel_ids(self) -> list[str]:
        return [c.strip() for c in self.slack_channels.split(",") if c.strip()]

    # ── Guards ───────────────────────────────────────────────────────────────
    def require_slack(self) -> None:
        if not self.slack_token:
            raise RuntimeError(
                "SLACK_TOKEN is not set. Create a Slack app, add channels:history + "
                "channels:read + users:read scopes, install it, and copy the token to .env."
            )

    def require_anthropic(self) -> None:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Get one at https://console.anthropic.com/ "
                "and add it to .env."
            )

    def ensure_dirs(self) -> None:
        for d in (self.raw_dir, self.enriched_dir, self.classified_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
