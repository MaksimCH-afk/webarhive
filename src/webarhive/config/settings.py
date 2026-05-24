from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global settings. Loaded from env / .env once at startup.

    UI can override per-run values; the orchestrator copies the active
    snapshot into the run record (spec §11) so history stays honest when
    different models are being tested.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Secrets
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")

    # DB
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/webarhive.db",
        alias="DATABASE_URL",
    )

    # LLM models per role (spec §9, §11) — never hardcoded, always a parameter
    model_classification: str = Field(default="openai/gpt-4o-mini", alias="MODEL_CLASSIFICATION")
    model_verdict: str = Field(default="openai/gpt-4o-mini", alias="MODEL_VERDICT")
    model_smart_drop: str = Field(default="openai/gpt-4o-mini", alias="MODEL_SMART_DROP")
    model_redirect: str = Field(default="openai/gpt-4o-mini", alias="MODEL_REDIRECT")

    # Role flags
    enable_verdict: bool = Field(default=True, alias="ENABLE_VERDICT")
    enable_smart_drop: bool = Field(default=False, alias="ENABLE_SMART_DROP")
    enable_redirect_llm: bool = Field(default=False, alias="ENABLE_REDIRECT_LLM")

    # Budgets and analysis thresholds
    max_llm_calls_per_domain: int = Field(default=40, alias="MAX_LLM_CALLS_PER_DOMAIN")
    cost_budget_per_domain: float = Field(default=0.5, alias="COST_BUDGET_PER_DOMAIN")
    text_limit: int = Field(default=2000, alias="TEXT_LIMIT")
    title_shift_threshold: int = Field(default=2, alias="TITLE_SHIFT_THRESHOLD")

    # Concurrency & throttling — IA is the bottleneck, single shared gate
    concurrency: int = Field(default=4, alias="CONCURRENCY")
    ia_rate_limit: float = Field(default=4.0, alias="IA_RATE_LIMIT")  # req/sec
    ia_backoff: float = Field(default=2.0, alias="IA_BACKOFF")  # base seconds
    ia_max_retries: int = Field(default=5, alias="IA_MAX_RETRIES")

    # Input
    check_subdomains: bool = Field(default=False, alias="CHECK_SUBDOMAINS")

    # App / deployment
    app_domain: str = Field(default="checker.local", alias="APP_DOMAIN")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    trust_proxy_headers: bool = Field(default=True, alias="TRUST_PROXY_HEADERS")

    # Computed
    @property
    def data_dir(self) -> Path:
        return Path("data")

    def snapshot(self) -> dict:
        """Settings snapshot copied into each run (spec §11)."""
        return {
            "models": {
                "classification": self.model_classification,
                "verdict": self.model_verdict,
                "smart_drop": self.model_smart_drop,
                "redirect": self.model_redirect,
            },
            "roles": {
                "verdict": self.enable_verdict,
                "smart_drop": self.enable_smart_drop,
                "redirect_llm": self.enable_redirect_llm,
            },
            "limits": {
                "max_llm_calls_per_domain": self.max_llm_calls_per_domain,
                "cost_budget_per_domain": self.cost_budget_per_domain,
                "text_limit": self.text_limit,
                "title_shift_threshold": self.title_shift_threshold,
            },
            "throttle": {
                "concurrency": self.concurrency,
                "ia_rate_limit": self.ia_rate_limit,
                "ia_backoff": self.ia_backoff,
                "ia_max_retries": self.ia_max_retries,
            },
            "input": {
                "check_subdomains": self.check_subdomains,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
