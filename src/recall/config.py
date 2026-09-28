"""Application settings. Every field is documented in .env.example; every field has a working default
except ANTHROPIC_API_KEY, which is required for any Claude call."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# USD per million tokens. Batch requests are billed at 50% of these rates; cache writes at 1.25x input,
# cache reads at 0.1x input. Override with LLM_PRICES_JSON when prices change.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- required for Claude calls ---
    anthropic_api_key: str = ""

    # --- core infrastructure ---
    database_url: str = "postgresql+asyncpg://recall:recall@localhost:5432/recall"
    run_migrations_on_startup: bool = True
    run_workers: bool = True
    worker_concurrency: int = 4
    scheduler_tick_seconds: int = 30
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # --- models (per route, see cost_controls) ---
    summarizer_standup_model: str = "claude-haiku-4-5-20251001"
    summarizer_model: str = "claude-sonnet-5"
    summarizer_effort: str = "medium"
    agent_model: str = "claude-sonnet-5"
    agent_effort: str = "low"
    agent_escalation_effort: str = "medium"
    router_model: str = "claude-haiku-4-5-20251001"
    agent_max_tool_calls: int = 4
    agent_max_tokens: int = 1024
    standup_haiku_max_minutes: int = 30
    use_batch_api: bool = True
    llm_prices_json: str = ""

    # --- embeddings ---
    embeddings_provider: Literal["local", "voyage", "hash"] = "local"
    embedding_dim: int = 384
    local_embedding_model: str = "BAAI/bge-small-en-v1.5"
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3.5"

    # --- speech to text (upload path only) ---
    stt_provider: Literal["auto", "deepgram", "whisper_local"] = "auto"
    deepgram_api_key: str = ""
    whisper_model: str = "small"

    # --- media storage ---
    media_storage: Literal["local", "azure_blob", "s3"] = "local"
    media_local_dir: str = "./data/media"
    azure_storage_account_url: str = ""
    azure_storage_container: str = "media"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""

    # --- Microsoft Graph ---
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_webhook_url: str = ""  # defaults to {public_base_url}/graph/notifications
    graph_client_state: str = "change-me"
    graph_poll_organizers: str = ""  # comma-separated Entra user ids whose meetings are polled
    graph_subscription_hours: int = 48

    # --- Teams bot ---
    bot_app_id: str = ""
    bot_app_password: str = ""
    bot_tenant_id: str = ""
    bot_default_service_url: str = "https://smba.trafficmanager.net/teams/"

    # --- API auth ---
    dev_auth: bool = False
    dev_token: str = "dev"
    service_token: str = ""
    entra_api_audience: str = ""  # app id URI / client id of the REST API registration

    # --- cost controls ---
    default_team_budget_usd: Decimal = Decimal("25")
    min_meeting_seconds: int = 180
    min_meeting_words: int = 300

    # --- observability ---
    applicationinsights_connection_string: str = ""
    otel_sample_ratio: float = 0.2

    # --- optional Azure-contained processing ---
    anthropic_provider: Literal["anthropic", "foundry"] = "anthropic"
    foundry_resource: str = ""
    foundry_api_key: str = ""

    # --- MCP ---
    mcp_user_email: str = ""

    @property
    def webhook_url(self) -> str:
        return self.graph_webhook_url or f"{self.public_base_url.rstrip('/')}/graph/notifications"

    @property
    def prices(self) -> dict[str, dict[str, float]]:
        prices = dict(DEFAULT_PRICES)
        if self.llm_prices_json:
            prices.update(json.loads(self.llm_prices_json))
        return prices

    @property
    def graph_enabled(self) -> bool:
        return bool(self.graph_tenant_id and self.graph_client_id and self.graph_client_secret)

    @property
    def bot_enabled(self) -> bool:
        return bool(self.bot_app_id and self.bot_app_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
