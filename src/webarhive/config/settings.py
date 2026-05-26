import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# UI-editable overrides land here. .env is the bootstrap default; once
# the operator changes anything in the UI, this JSON file takes over.
# Spec §15: "all parameters of the config are managed from the UI".
# Spec §11: env file remains for secrets + startup defaults.
OVERRIDES_PATH = Path("data/settings.json")

# Editable parameter set — what the UI is allowed to override (everything
# except deployment-time bindings like the listen host/port). API keys
# are editable from the UI too (user explicitly asked for it) — kept in
# data/settings.json which is gitignored, so secrets don't leak via git.
_EDITABLE_FIELDS = {
    "openrouter_api_key",
    "model_classification", "model_verdict", "model_smart_drop", "model_redirect",
    "enable_verdict", "enable_smart_drop", "enable_redirect_llm",
    "max_llm_calls_per_domain", "cost_budget_per_domain",
    "text_limit", "title_shift_threshold", "light_fetch_cap",
    "redirect_cap", "redirect_llm_review_cap",
    "concurrency", "ia_rate_limit", "ia_backoff", "ia_max_retries",
    "per_domain_timeout_sec",
    "check_subdomains",
    # WHOIS
    "whois_api_key", "whois_enabled", "whois_rate_limit",
    "whois_cache_ttl_days", "whois_monthly_floor",
    # Best snapshot
    "enable_best_snapshot", "best_snapshot_top_n",
    "enable_best_snapshot_content_llm",
}


class Settings(BaseSettings):
    """Global settings. Loaded from env / .env once at startup, then
    file-overridden from `data/settings.json` if it exists.

    UI edits write to that JSON file; the orchestrator copies the
    active snapshot into the run record (spec §11) so history stays
    honest when different models are being tested.
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
    # Доменов с архивом по 1500+ версий гонять light fetch на каждую —
    # нереалистично (IA throttle: часы). Сэмплируем до этого числа.
    light_fetch_cap: int = Field(default=120, alias="LIGHT_FETCH_CAP")
    # Сколько 3xx-снапшотов реально проверять. На доменах с 2000+
    # редиректами фетч каждого через IA throttle = десятки минут.
    redirect_cap: int = Field(default=150, alias="REDIRECT_CAP")
    # Сверх скольких REVIEW-редиректов отключаем CDX-обогащение цели
    # в llm_refine_redirects (слишком дорого).
    redirect_llm_review_cap: int = Field(default=30, alias="REDIRECT_LLM_REVIEW_CAP")
    # Жёсткий потолок времени на один домен. При превышении воркер
    # помечает домен ERROR и идёт дальше, не зависая на пуле.
    per_domain_timeout_sec: int = Field(default=1800, alias="PER_DOMAIN_TIMEOUT_SEC")

    # Concurrency & throttling — IA is the bottleneck, single shared gate
    concurrency: int = Field(default=4, alias="CONCURRENCY")
    ia_rate_limit: float = Field(default=4.0, alias="IA_RATE_LIMIT")  # req/sec
    ia_backoff: float = Field(default=2.0, alias="IA_BACKOFF")  # base seconds
    ia_max_retries: int = Field(default=5, alias="IA_MAX_RETRIES")

    # Input
    check_subdomains: bool = Field(default=False, alias="CHECK_SUBDOMAINS")

    # WHOIS (spec extension) — реальная дата регистрации домена через WhoisJSON
    whois_api_key: str = Field(default="", alias="WHOIS_API_KEY")
    whois_enabled: bool = Field(default=False, alias="WHOIS_ENABLED")
    whois_rate_limit: float = Field(default=20.0 / 60.0, alias="WHOIS_RATE_LIMIT")  # req/sec → 20/min
    whois_cache_ttl_days: int = Field(default=90, alias="WHOIS_CACHE_TTL_DAYS")
    # Если месячный остаток (Remaining-Requests) опустится ниже floor —
    # перестаём дёргать API на текущий прогон.
    whois_monthly_floor: int = Field(default=10, alias="WHOIS_MONTHLY_FLOOR")

    # Best snapshot (spec extension) — лучший слепок на эпоху по полноте
    # ресурсов. По умолчанию выключен — фичу включает оператор.
    enable_best_snapshot: bool = Field(default=False, alias="ENABLE_BEST_SNAPSHOT")
    best_snapshot_top_n: int = Field(default=5, alias="BEST_SNAPSHOT_TOP_N")
    enable_best_snapshot_content_llm: bool = Field(default=False, alias="ENABLE_BEST_SNAPSHOT_CONTENT_LLM")

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

    def editable_fields(self) -> dict[str, Any]:
        """Current values of UI-editable fields, for rendering the form."""
        return {name: getattr(self, name) for name in _EDITABLE_FIELDS}

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
                "light_fetch_cap": self.light_fetch_cap,
                "redirect_cap": self.redirect_cap,
                "redirect_llm_review_cap": self.redirect_llm_review_cap,
            },
            "throttle": {
                "concurrency": self.concurrency,
                "ia_rate_limit": self.ia_rate_limit,
                "ia_backoff": self.ia_backoff,
                "ia_max_retries": self.ia_max_retries,
                "per_domain_timeout_sec": self.per_domain_timeout_sec,
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
            "whois": {
                "enabled": self.whois_enabled,
                "rate_limit": self.whois_rate_limit,
                "cache_ttl_days": self.whois_cache_ttl_days,
                "monthly_floor": self.whois_monthly_floor,
            },
            "best_snapshot": {
                "enabled": self.enable_best_snapshot,
                "top_n": self.best_snapshot_top_n,
                "content_llm": self.enable_best_snapshot_content_llm,
            },
        }


def _load_overrides() -> dict[str, Any]:
    if not OVERRIDES_PATH.is_file():
        return {}
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Whitelist + drop unknown keys (forward-compat).
    return {k: v for k, v in raw.items() if k in _EDITABLE_FIELDS}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    overrides = _load_overrides()
    if overrides:
        # Pydantic v2: model_copy(update=...) returns a new instance with
        # overridden fields. We keep secrets and deployment fields intact.
        s = s.model_copy(update=overrides)
    return s


def save_overrides(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist UI edits to OVERRIDES_PATH and bust the settings cache.

    Returns the dict that ended up being saved (with type coercions).
    Only fields in _EDITABLE_FIELDS are accepted.
    """
    current = _load_overrides()
    coerced: dict[str, Any] = {}
    for k, v in updates.items():
        if k not in _EDITABLE_FIELDS:
            continue
        coerced[k] = v
    merged = {**current, **coerced}
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    get_settings.cache_clear()
    return merged
